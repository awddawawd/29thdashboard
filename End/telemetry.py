"""Moteur de télémétrie WaaS (collecte en mémoire + sondes réseau durcies).

Ce module gère le worker d'arrière-plan qui sonde les conteneurs Docker locaux,
exécute des sondes HTTP/TCP vers des cibles tierces et agrège des métriques en
mémoire vive pour des endpoints HTTP rapides.

Durcissements appliqués (revue de sécurité) :

* **Anti-SSRF** : classification ``ipaddress`` exhaustive (IPv4 mappées, 6to4,
  Teredo, NAT64, site-local, plages spéciales) + politique de ports
  allowlist/denylist configurable en base ;
* **Anti-DNS-rebinding** : épinglage de l'IP au niveau du backend réseau
  (``connect_tcp``), *lié à l'hôte* et fail-closed ;
* **TLS** : le contexte SSL calculé par httpx est explicitement transmis au pool
  httpcore (httpcore conserve ``server_hostname`` d'origine -> SNI/SAN intacts) ;
* **Ressources** : sondes en streaming (aucun corps matérialisé en RAM),
  concurrence bornée, exécuteurs dédiés, résolveur DNS auto-réparant à threads
  daemon (aucun blocage de l'arrêt du process) ;
* **Robustesse** : collecte best-effort (une cible défaillante ne fige plus la
  télémétrie), coercitions sûres de toutes les valeurs venant de la base.
"""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import contextlib
import contextvars
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import functools
import ipaddress
import logging
import os
from pathlib import Path
import queue
import random
import shutil
import socket
import sys
import threading
import time
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse

import docker
from docker.errors import APIError, DockerException, NotFound
import httpcore
import httpx

import db

try:  # Mesures de l'hôte : psutil si disponible, sinon lecture système partielle.
    import psutil  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - dépendance optionnelle
    psutil = None

logger = logging.getLogger("waas.telemetry")

# ---------------------------------------------------------------------------
# Constantes de politique réseau
# ---------------------------------------------------------------------------

BLOCKED_IP_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("192.88.99.0/24"),      # relais anycast 6to4
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("255.255.255.255/32"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::ffff:0:0/96"),       # IPv4-mapped
    ipaddress.ip_network("64:ff9b::/96"),        # NAT64 (-> 127.0.0.1 possible)
    ipaddress.ip_network("100::/64"),            # discard-only
    ipaddress.ip_network("2001::/32"),           # Teredo
    ipaddress.ip_network("2001:20::/28"),        # ORCHIDv2
    ipaddress.ip_network("2001:db8::/32"),
    ipaddress.ip_network("2002::/16"),           # 6to4
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fec0::/10"),           # site-local déprécié
)

#: Ports de contrôle/administration/données interdits par défaut (mode denylist).
DEFAULT_BLOCKED_PORTS: frozenset[int] = frozenset({
    2375, 2376, 2377, 4243, 4789, 7946,                       # Docker / Swarm
    2379, 2380, 4001, 7001,                                   # etcd
    6443, 10250, 10255, 10256, 10257, 10259,                  # Kubernetes
    8500, 8501, 8200, 8201, 4646, 4647,                       # Consul / Vault / Nomad
    3306, 5432, 5433, 1433, 1521,                             # SGBD
    6379, 6380, 11211,                                        # Redis / Memcached
    27017, 27018, 27019, 28017,                               # MongoDB
    9200, 9300, 5984, 9042, 9160,                             # Elastic / CouchDB / Cassandra
    2181, 9092, 5672, 15672, 61616, 61613, 1883, 8883,        # ZK / Kafka / AMQP / MQTT
})

ALLOWED_HTTP_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})

#: Ports internes exposant le serveur web d'un conteneur locataire : ``8080`` pour
#: les images durcies (nginx non privilégié), ``80`` pour les conteneurs
#: historiques encore en service pendant la migration.
WEB_PORT_KEYS = ("8080/tcp", "80/tcp")

#: Durée maximale d'une résolution DNS (le thread reste borné par le pool).
DNS_TIMEOUT = 2.5
DNS_POOL_MIN_WORKERS = 2
DNS_POOL_MAX_WORKERS = 48
DNS_POOL_IDLE_TIMEOUT = 30.0

#: Pool partagé des sondes. Les sondes envoient ``Connection: close`` et ne lisent
#: jamais le corps de la réponse : la connexion n'est donc pas réutilisée (aucun
#: état résiduel entre deux cibles), et ces limites ne servent qu'à borner le
#: nombre de sockets simultanées.
HTTP_LIMITS = httpx.Limits(max_connections=50, max_keepalive_connections=25, keepalive_expiry=30.0)

_PUBLIC_SSRF_ERROR = "Cible refusée par la politique de sécurité réseau."
_PUBLIC_UNREACHABLE_ERROR = "Cible injoignable (réseau ou résolution DNS)."
_PUBLIC_BUSY_ERROR = "Résolveur DNS temporairement saturé, réessai au prochain cycle."

_SSRF_PREFIX = "Sécurité : "
_USER_AGENT = "waas-telemetry/1.0 (+https://waas.local)"

#: Épinglage anti-DNS-rebinding : (hôte validé, IP validée) — jamais seulement l'IP.
_pinned_ip_ctx: contextvars.ContextVar[Optional[tuple[str, str]]] = contextvars.ContextVar(
    "pinned_target", default=None
)


# ---------------------------------------------------------------------------
# Utilitaires sans I/O
# ---------------------------------------------------------------------------

def _snapshot(value: Any) -> Any:
    """Copie défensive récursive d'un instantané exposé aux routes HTTP."""
    if isinstance(value, dict):
        return {k: _snapshot(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_snapshot(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_snapshot(v) for v in value)
    return value


def as_float(value: Any, default: float) -> float:
    """Conversion ``float`` tolérante (None, chaîne vide, format FR, valeur sale)."""
    if isinstance(value, str):
        value = value.replace("\u202f", "").replace("\u00a0", "").replace(" ", "").replace(",", ".")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed or parsed in (float("inf"), float("-inf")):  # NaN / inf
        return default
    return parsed


def as_int(value: Any, default: int) -> int:
    """Conversion ``int`` tolérante."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "oui", "vrai")


def _parse_port_set(raw: Any) -> frozenset[int]:
    ports: set[int] = set()
    for chunk in str(raw or "").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        port = as_int(chunk, -1)
        if 1 <= port <= 65535:
            ports.add(port)
    return frozenset(ports)


def is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True si l'adresse ne doit jamais être contactée par une sonde externe."""
    if (
        not ip.is_global          # inclut 10/8, 127/8, 169.254/16, 192.168/16, ::/128…
        or ip.is_multicast        # is_global est vrai pour 224.0.0.0/4 (piège Python)
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
        or getattr(ip, "is_site_local", False)      # fec0::/10
    ):
        return True

    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return is_forbidden_ip(mapped)

    sixtofour = getattr(ip, "sixtofour", None)
    if sixtofour is not None and is_forbidden_ip(sixtofour):
        return True

    teredo = getattr(ip, "teredo", None)
    if teredo is not None and is_forbidden_ip(teredo[1]):
        return True

    return any(ip.version == net.version and ip in net for net in BLOCKED_IP_NETWORKS)


def calculate_cpu_percent(stats: dict[str, Any], online_cpus: Optional[int] = None) -> float:
    """Pourcentage CPU instantané à partir d'un échantillon ``docker stats``."""
    try:
        cpu_stats = stats.get("cpu_stats") or {}
        precpu_stats = stats.get("precpu_stats") or {}

        cpu_usage = (cpu_stats.get("cpu_usage") or {}).get("total_usage")
        precpu_usage = (precpu_stats.get("cpu_usage") or {}).get("total_usage")
        system_usage = cpu_stats.get("system_cpu_usage")
        presystem_usage = precpu_stats.get("system_cpu_usage")
        if None in (cpu_usage, precpu_usage, system_usage, presystem_usage):
            return 0.0

        cpu_delta = cpu_usage - precpu_usage
        system_delta = system_usage - presystem_usage

        if online_cpus is None:
            online_cpus = cpu_stats.get("online_cpus") or len(
                (cpu_stats.get("cpu_usage") or {}).get("percpu_usage") or []
            ) or 1
        online_cpus = max(as_int(online_cpus, 1), 1)

        if system_delta > 0 and cpu_delta > 0:
            return round((cpu_delta / system_delta) * online_cpus * 100.0, 2)
        return 0.0
    except Exception:  # structure inattendue : on ne casse jamais la collecte
        logger.debug("calculate_cpu_percent : échantillon inexploitable", exc_info=True)
        return 0.0


# ---------------------------------------------------------------------------
# Politique de sondage (chargée hors event loop, lue sans I/O)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProbePolicy:
    blocked_ports: frozenset[int]
    allowed_ports: Optional[frozenset[int]]
    allowed_methods: frozenset[str]
    probe_concurrency: int
    container_probe_concurrency: int
    max_monitors_per_collection: int
    block_self_targeting: bool
    self_hosts: frozenset[str]
    self_ips: frozenset[str]
    loaded_at: float = 0.0
    source: str = "defaults"

    def port_allowed(self, port: int) -> Optional[str]:
        """None si autorisé, sinon motif de refus."""
        if self.allowed_ports is not None:
            if port in self.allowed_ports:
                return None
            return (
                f"Port {port} hors de la liste blanche des sondes externes "
                f"({', '.join(str(p) for p in sorted(self.allowed_ports))})."
            )
        if port in self.blocked_ports:
            return f"Port {port} interdit pour les sondes externes (service interne réservé)."
        return None

    def is_self_target(self, host: str, ips: Iterable[str]) -> bool:
        if not self.block_self_targeting:
            return False
        if host and host.lower() in self.self_hosts:
            return True
        return any(ip in self.self_ips for ip in ips)


def _default_policy() -> ProbePolicy:
    return ProbePolicy(
        blocked_ports=DEFAULT_BLOCKED_PORTS,
        allowed_ports=None,
        allowed_methods=ALLOWED_HTTP_METHODS,
        probe_concurrency=10,
        container_probe_concurrency=20,
        max_monitors_per_collection=0,          # 0 = illimité
        block_self_targeting=True,
        self_hosts=frozenset(),
        self_ips=frozenset(),
    )


_POLICY: ProbePolicy = _default_policy()
_POLICY_TTL_SECONDS = 60.0


def _policy_is_stale() -> bool:
    return (time.monotonic() - _POLICY.loaded_at) > _POLICY_TTL_SECONDS


def get_policy() -> ProbePolicy:
    """Politique courante (lecture mémoire pure, jamais d'I/O)."""
    return _POLICY


def _local_fingerprint(api_public_url: str, base_domain: str) -> tuple[set[str], set[str]]:
    """Hôtes/IPs désignant la plateforme elle-même (anti auto-sondage)."""
    hosts: set[str] = set()
    ips: set[str] = set()

    for raw in (api_public_url, base_domain):
        value = (raw or "").strip()
        if not value:
            continue
        host = urlparse(value if "://" in value else f"//{value}").hostname
        if not host:
            continue
        host = host.lower()
        if host in ("localhost", "127.0.0.1", "::1"):
            continue
        hosts.add(host)

    candidates = set(hosts) | {socket.gethostname()}
    for host in candidates:
        try:
            for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM):
                ip_str = info[4][0]
                try:
                    ip = ipaddress.ip_address(ip_str)
                except ValueError:
                    continue
                if not ip.is_loopback:
                    ips.add(ip_str)
        except Exception:  # résolution impossible : simple perte de précision
            continue

    return hosts, ips


def load_probe_policy_sync() -> ProbePolicy:
    """Charge la politique depuis ``fleet_settings``. **Fonction bloquante** :
    à exécuter via un exécuteur dédié, jamais dans l'event loop."""
    global _POLICY

    setting = _setting_reader()
    allowlist = _parse_port_set(setting("telemetry_probe_port_allowlist", ""))
    extra_blocked = _parse_port_set(setting("telemetry_blocked_probe_ports", ""))
    methods_raw = setting("telemetry_allowed_http_methods", "")
    methods = {
        token.strip().upper()
        for token in str(methods_raw or "").replace(";", ",").split(",")
        if token.strip()
    } or set(ALLOWED_HTTP_METHODS)

    block_self = as_bool(setting("telemetry_block_self_targeting", "1"), True)
    api_public_url = setting("api_public_url", "")
    base_domain = setting("client_base_domain", "")

    if block_self:
        self_hosts, self_ips = _local_fingerprint(api_public_url, base_domain)
    else:
        self_hosts, self_ips = set(), set()

    policy = ProbePolicy(
        blocked_ports=DEFAULT_BLOCKED_PORTS | extra_blocked,
        allowed_ports=allowlist or None,
        allowed_methods=frozenset(methods),
        probe_concurrency=max(1, min(as_int(setting("telemetry_probe_concurrency", 10), 10), 50)),
        container_probe_concurrency=max(
            1, min(as_int(setting("telemetry_container_probe_concurrency", 20), 20), 100)
        ),
        max_monitors_per_collection=max(
            0, as_int(setting("telemetry_max_monitors_per_collection", 0), 0)
        ),
        block_self_targeting=block_self,
        self_hosts=frozenset(self_hosts),
        self_ips=frozenset(self_ips),
        loaded_at=time.monotonic(),
        source="fleet_settings",
    )
    _POLICY = policy
    return policy


def _setting_reader() -> Callable[[str, Any], Any]:
    """Lecteur de réglages tolérant aux pannes de la base (retombe sur les défauts)."""
    def read(key: str, default: Any = "") -> Any:
        try:
            return db.get_setting(key, default)
        except Exception:
            logger.warning("Lecture du réglage '%s' impossible, valeur par défaut utilisée", key)
            return default

    return read


# ---------------------------------------------------------------------------
# Résolveur DNS : pool de threads daemon auto-réparant (aucun thread orphelin
# non borné, aucun blocage de l'arrêt du process)
# ---------------------------------------------------------------------------

class DnsThreadPool:
    """Pool minimaliste à threads **daemon**, élastique et auto-réparant.

    Contrairement à ``ThreadPoolExecutor`` :

    * les threads sont daemon : un ``getaddrinfo`` gelé ne bloque plus l'arrêt du
      process (``concurrent.futures.thread._python_exit`` n'attend plus) ;
    * un thread bloqué est simplement *remplacé* (jusqu'à ``max_workers``), ce qui
      empêche la famine définitive des résolutions suivantes ;
    * au-delà du plafond, les soumissions sont refusées immédiatement
      (disjoncteur) au lieu d'être mises en file derrière des threads morts.
    """

    def __init__(
        self,
        min_workers: int = DNS_POOL_MIN_WORKERS,
        max_workers: int = DNS_POOL_MAX_WORKERS,
        idle_timeout: float = DNS_POOL_IDLE_TIMEOUT,
        name_prefix: str = "waas-dns",
    ) -> None:
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._total = 0        # threads vivants
        self._busy = 0         # jobs réservés (en file ou en cours)
        self._min_workers = max(1, min_workers)
        self._max_workers = max(self._min_workers, max_workers)
        self._idle_timeout = idle_timeout
        self._name_prefix = name_prefix
        self._stopped = False
        self.rejected = 0
        self.timed_out = 0
        self.completed = 0

    # -- API publique ------------------------------------------------------
    def submit(self, host: str, port: int) -> Optional[concurrent.futures.Future]:
        """Retourne un futur, ou ``None`` si le pool est saturé (fail-fast)."""
        with self._lock:
            if self._stopped:
                return None
            spawn = False
            if self._total - self._busy <= 0:
                if self._total >= self._max_workers:
                    self.rejected += 1
                    return None
                spawn = True
                self._total += 1
            self._busy += 1

        future: concurrent.futures.Future = concurrent.futures.Future()
        self._queue.put((future, host, port))
        if spawn:
            threading.Thread(
                target=self._worker,
                name=f"{self._name_prefix}-{self._total}",
                daemon=True,
            ).start()
        return future

    def shutdown(self) -> None:
        self._stopped = True

    @property
    def stopped(self) -> bool:
        return self._stopped

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "threads": self._total,
                "busy": self._busy,
                "max_threads": self._max_workers,
                "rejected": self.rejected,
                "timed_out": self.timed_out,
                "completed": self.completed,
            }

    # -- Worker ------------------------------------------------------------
    def _worker(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=self._idle_timeout)
            except queue.Empty:
                with self._lock:
                    if self._stopped or (self._total - self._busy) > self._min_workers:
                        self._total -= 1
                        return
                continue

            future, host, port = item
            should_exit = False
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM))
                    except BaseException as exc:  # noqa: BLE001 — transmis à l'appelant
                        future.set_exception(exc)
            finally:
                with self._lock:
                    self._busy -= 1
                    self.completed += 1
                    if self._stopped and (self._total - self._busy) > 0:
                        self._total -= 1
                        should_exit = True
            if should_exit:
                return


_dns_pool = DnsThreadPool()
_dns_pool_lock = threading.Lock()


def get_dns_pool() -> DnsThreadPool:
    global _dns_pool
    with _dns_pool_lock:
        if _dns_pool.stopped:                        # redémarrage à chaud
            _dns_pool = DnsThreadPool()
        return _dns_pool


class DnsOverloaded(RuntimeError):
    """Le résolveur est saturé : mieux vaut refuser que de bloquer."""


async def resolve_addrinfo(host: str, port: int, timeout: float = DNS_TIMEOUT) -> list[Any]:
    """Résolution annulable côté appelant, bornée côté threads."""
    pool = get_dns_pool()
    future = pool.submit(host, port)
    if future is None:
        raise DnsOverloaded(_PUBLIC_BUSY_ERROR)

    loop = asyncio.get_running_loop()
    wrapped = asyncio.wrap_future(future, loop=loop)
    try:
        return await asyncio.wait_for(wrapped, timeout=timeout)
    except asyncio.TimeoutError:
        pool.timed_out += 1
        raise


# ---------------------------------------------------------------------------
# Validation anti-SSRF
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SafeTarget:
    host: str
    port: int
    scheme: str
    ips: tuple[str, ...]
    is_url: bool


async def resolve_safe_target(target: str, is_url: bool = True) -> tuple[Optional[SafeTarget], Optional[str]]:
    """Valide une cible externe et retourne ses IP publiques sûres.

    Retourne ``(SafeTarget, None)`` ou ``(None, motif_de_refus)``.
    """
    raw = (target or "").strip()
    if not raw:
        return None, "Cible vide."

    scheme = "http"
    try:
        if is_url:
            parsed = urlparse(raw if "://" in raw else f"http://{raw}")
            scheme = (parsed.scheme or "").lower()
            host = parsed.hostname
            if not host:
                return None, "Nom d'hôte invalide ou absent."
            if scheme not in ("http", "https"):
                return None, f"Protocole '{parsed.scheme}' non autorisé."
            try:
                port = parsed.port or (443 if scheme == "https" else 80)
            except ValueError:
                return None, "Port hors plage (0-65535)."
            if not (1 <= port <= 65535):
                return None, "Port hors plage (0-65535)."
        else:
            host, port, parse_error = _parse_tcp_target(raw)
            if host is None or port is None:
                return None, parse_error or "Cible TCP invalide."
    except Exception as exc:  # parsing défensif
        return None, f"Cible illisible : {type(exc).__name__}."

    policy = get_policy()
    port_error = policy.port_allowed(port)
    if port_error:
        return None, port_error

    try:
        addr_info = await resolve_addrinfo(host, port)
    except DnsOverloaded as exc:
        return None, str(exc)
    except asyncio.TimeoutError:
        return None, "Délai de résolution DNS dépassé (Timeout)."
    except socket.gaierror as exc:
        return None, f"Échec de résolution DNS : {exc}"
    except Exception as exc:
        return None, f"Résolution DNS impossible : {type(exc).__name__}."

    if not addr_info:
        return None, "Résolution DNS impossible pour cette cible."

    valid_ips: list[str] = []
    for entry in addr_info:
        ip_str = entry[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return None, f"Adresse IP résolue invalide : {ip_str}"

        if is_forbidden_ip(ip):
            return None, (
                f"Accès interdit aux adresses privées, réservées ou internes "
                f"(SSRF bloqué : {ip_str})."
            )
        if ip_str not in valid_ips:
            valid_ips.append(ip_str)

    if not valid_ips:
        return None, "Aucune adresse IP publique valide trouvée."

    if policy.is_self_target(host, valid_ips):
        return None, "Cible interne à la plateforme : l'auto-sondage est interdit."

    # IPv4 d'abord (compatibilité maximale), puis IPv6.
    valid_ips.sort(key=lambda item: ":" in item)
    return SafeTarget(host=host, port=port, scheme=scheme,
                      ips=tuple(valid_ips), is_url=is_url), None


def _parse_tcp_target(target: str) -> tuple[Optional[str], Optional[int], Optional[str]]:
    """Analyse ``host:port`` / ``[ipv6]:port`` et retourne ``(hôte, port, motif)``."""
    clean = target.strip()
    host: Optional[str] = None
    port_str: Optional[str] = None

    if clean.startswith("["):
        closing = clean.find("]")
        if closing == -1:
            return None, None, "Crochet IPv6 non fermé (format attendu [adresse]:port)."
        host = clean[1:closing].strip()
        remainder = clean[closing + 1:].strip()
        if not remainder.startswith(":"):
            return None, None, "Port TCP requis (format [adresse]:port)."
        port_str = remainder[1:].strip()
    elif clean.count(":") >= 1:
        # IPv4:port, hostname:port, ou IPv6 littéral sans crochets (dernière section = port).
        host, port_str = (part.strip() for part in clean.rsplit(":", 1))
    else:
        return None, None, "Port TCP requis (format host:port ou [ipv6]:port)."

    if not host:
        return None, None, "Hôte TCP invalide."

    try:
        port = int(port_str)
    except (TypeError, ValueError):
        return None, None, f"Port TCP invalide : '{port_str}'."

    if not (1 <= port <= 65535):
        return None, None, f"Port TCP invalide : {port}."

    return host, port, None


async def is_safe_external_target(
    target: str,
    is_url: bool = True,
) -> tuple[bool, Optional[str], Optional[str], Optional[int]]:
    """Compatibilité : ``(sûr, message, première IP, port)``."""
    safe_target, error = await resolve_safe_target(target, is_url=is_url)
    if safe_target is None:
        return False, error, None, None
    return True, None, safe_target.ips[0], safe_target.port


# ---------------------------------------------------------------------------
# Transport HTTPX avec épinglage d'IP (anti-DNS-rebinding)
# ---------------------------------------------------------------------------

class DynamicPinnedIPNetworkBackend(httpcore.AnyIOBackend):
    """Backend réseau qui substitue l'hôte par l'IP validée **de cette cible**.

    Le pin est *lié à l'hôte* : toute connexion vers un autre hôte que la cible
    épinglée est refusée (fail-closed), ce qui empêche qu'un contexte hérité
    détourne une sonde locale vers une IP publique.
    """

    async def connect_tcp(self, host: str, port: int, *args, **kwargs):
        pinned = _pinned_ip_ctx.get()
        if pinned is not None:
            pinned_host, pinned_ip = pinned
            if host.rstrip(".").lower() != pinned_host.rstrip(".").lower():
                raise httpcore.ConnectError(
                    f"Connexion refusée : '{host}:{port}' hors de la cible épinglée "
                    f"'{pinned_host}' (protection anti-DNS-rebinding)."
                )
            host = pinned_ip
        return await super().connect_tcp(host, port, *args, **kwargs)


def build_pinned_transport(**kwargs: Any) -> "DynamicPinnedIPTransport":
    return DynamicPinnedIPTransport(**kwargs)


class DynamicPinnedIPTransport(httpx.AsyncHTTPTransport):
    """Transport httpx dont le pool httpcore réutilise toute la configuration TLS.

    ``limits`` est optionnel côté API publique (contrairement à httpx) et un pool
    unique est construit avec le ``ssl_context`` calculé par httpx, sans jamais
    laisser de pool orphelin.
    """

    def __init__(self, **kwargs: Any) -> None:
        limits = kwargs.pop("limits", None) or HTTP_LIMITS
        verify = kwargs.get("verify", True)
        cert = kwargs.get("cert")
        trust_env = kwargs.get("trust_env", True)
        http2 = bool(kwargs.get("http2", False))

        kwargs["limits"] = limits
        # Le pool construit par httpx est volontairement remplacé juste après : il
        # n'est jamais démarré (aucun descripteur, aucun thread) et sert uniquement
        # à valider les paramètres comme le fait httpx.
        super().__init__(**kwargs)

        # Contexte SSL identique à celui que httpx aurait utilisé (certifi / SSL_CERT_FILE,
        # verify=False, contexte explicite…) : httpcore s'en sert pour le SNI et la
        # vérification du certificat d'origine, l'IP épinglée ne sert qu'au TCP.
        ssl_context = httpx.create_ssl_context(
            verify=verify, cert=cert, trust_env=trust_env
        )
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,                      # ← configuration TLS conservée
            network_backend=DynamicPinnedIPNetworkBackend(),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=kwargs.get("http1", True),
            http2=http2,
            retries=as_int(kwargs.get("retries", 0), 0),
            local_address=kwargs.get("local_address"),
            socket_options=kwargs.get("socket_options"),
        )

    def __repr__(self) -> str:  # lisibilité des logs
        return "DynamicPinnedIPTransport(network_backend=DynamicPinnedIPNetworkBackend)"


def build_timeout(total: float, connect: Optional[float] = None) -> httpx.Timeout:
    """Timeout cohérent : la file du pool ne doit pas créer de faux 'DOWN'."""
    total = max(0.5, min(float(total), 60.0))
    return httpx.Timeout(
        total,
        connect=min(connect if connect is not None else total, 5.0),
        read=total,
        write=total,
        pool=min(max(total, 2.0), 15.0),
    )


def sanitize_headers(headers: Optional[dict[str, str]]) -> dict[str, str]:
    """En-têtes additionnels : jamais les en-têtes de contrôle hop-by-hop."""
    safe: dict[str, str] = {"Connection": "close"}
    forbidden = {"host", "connection", "content-length", "transfer-encoding", "upgrade", "te"}
    for key, value in (headers or {}).items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if key.strip().lower() in forbidden or "\r" in value or "\n" in value:
            continue
        safe[key] = value
    return safe


# ---------------------------------------------------------------------------
# Sondes
# ---------------------------------------------------------------------------

async def _http_probe(
    url: str,
    client: Optional[httpx.AsyncClient],
    timeout: float,
    *,
    method: str = "GET",
    expected_status: int = 200,
    headers: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Sonde HTTP unique, mutualisée par les chemins local et externe.

    Le corps de la réponse n'est **jamais** lu (streaming) : une cible hostile ne
    peut pas saturer la mémoire du service.
    """
    policy = get_policy()
    method = str(method or "GET").upper()
    if method not in policy.allowed_methods:
        logger.warning("Méthode '%s' non autorisée pour une sonde : GET utilisé", method)
        method = "GET"

    req_headers = sanitize_headers(headers)
    timeout_obj = build_timeout(timeout)

    created_client: Optional[httpx.AsyncClient] = None
    active_client = client
    if active_client is None:
        created_client = httpx.AsyncClient(
            transport=DynamicPinnedIPTransport(limits=HTTP_LIMITS, verify=True),
            timeout=timeout_obj,
            follow_redirects=False,
            headers={"User-Agent": _USER_AGENT},
        )
        active_client = created_client

    t0 = time.monotonic()
    try:
        async with active_client.stream(
            method, url, headers=req_headers, timeout=timeout_obj, follow_redirects=False
        ) as resp:
            latency = round((time.monotonic() - t0) * 1000, 1)
            status_code = resp.status_code

        if status_code in (301, 302, 303, 307, 308):
            is_expected = status_code == expected_status
            return {
                "status": "UP" if is_expected else "DEGRADED",
                "latency_ms": latency,
                "status_code": status_code,
                "expected": expected_status,
                "error": None if is_expected else (
                    f"Redirection HTTP ({status_code}) non suivie (protection anti-SSRF)."
                ),
                "error_public": None if is_expected else f"Redirection HTTP ({status_code}) non suivie.",
            }

        is_ok = status_code == expected_status
        return {
            "status": "UP" if is_ok else "DEGRADED",
            "latency_ms": latency,
            "status_code": status_code,
            "expected": expected_status,
            "error": None if is_ok else f"Code HTTP reçu : {status_code}",
            "error_public": None if is_ok else f"Code HTTP reçu : {status_code}",
        }
    except Exception as exc:
        logger.debug("Sonde HTTP %s échouée : %s", url, exc)
        return {
            "status": "DOWN",
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "status_code": None,
            "error": f"{type(exc).__name__}: {exc}",
            "error_public": _PUBLIC_UNREACHABLE_ERROR,
        }
    finally:
        if created_client is not None:
            with contextlib.suppress(Exception):
                await created_client.aclose()


async def probe_local_http(
    host_port: int | str,
    client: Optional[httpx.AsyncClient] = None,
    timeout: float = 1.5,
) -> dict[str, Any]:
    """Sonde d'un service **local** (loopback) : cible fixée par le code.

    Aucune validation SSRF ici : l'entrée est un simple numéro de port, jamais
    une URL, ce qui interdit tout détournement de ce chemin non validé.
    """
    port = as_int(host_port, 0)
    if not (1 <= port <= 65535):
        return {
            "status": "DOWN",
            "latency_ms": None,
            "status_code": None,
            "error": f"Port local invalide : {host_port!r}",
            "error_public": _PUBLIC_UNREACHABLE_ERROR,
        }
    return await _http_probe(f"http://127.0.0.1:{port}", client, timeout)


async def probe_local_container(
    host_port: int | str,
    client: Optional[httpx.AsyncClient],
    timeout: float = 1.5,
) -> dict[str, Any]:
    """Alias historique conservé pour la collecte de santé de flotte."""
    return await probe_local_http(host_port, client=client, timeout=timeout)


async def probe_external_http(
    target: str,
    method: str = "GET",
    expected_status: int = 200,
    timeout: float = 3.0,
    client: Optional[httpx.AsyncClient] = None,
    headers: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Sonde HTTP d'une cible **tierce** : validation SSRF + épinglage d'IP."""
    safe_target, error_msg = await resolve_safe_target(target, is_url=True)
    if safe_target is None:
        logger.warning("Sonde externe refusée pour %r : %s", target, error_msg)
        return {
            "status": "DOWN",
            "latency_ms": None,
            "status_code": None,
            "error": f"{_SSRF_PREFIX}{error_msg}",
            "error_public": _PUBLIC_SSRF_ERROR,
        }

    normalized_url = target if "://" in target else f"http://{target}"
    expected_status = as_int(expected_status, 200)

    last_result: Optional[dict[str, Any]] = None
    for candidate in safe_target.ips[:2]:
        token = _pinned_ip_ctx.set((safe_target.host, candidate))
        try:
            result = await _http_probe(
                normalized_url,
                client,
                as_float(timeout, 3.0),
                method=method,
                expected_status=expected_status,
                headers=headers,
            )
        finally:
            _pinned_ip_ctx.reset(token)

        if result["status"] == "UP":
            return result
        last_result = result
        # Une erreur applicative (statut inattendu) ne justifie pas de changer d'IP.
        if result.get("status_code") is not None:
            return result

    return last_result or {
        "status": "DOWN",
        "latency_ms": None,
        "status_code": None,
        "error": _PUBLIC_UNREACHABLE_ERROR,
        "error_public": _PUBLIC_UNREACHABLE_ERROR,
    }


async def probe_tcp(target: str, timeout: float = 2.5) -> dict[str, Any]:
    """Sonde TCP : connexion sur l'IP **validée** (jamais de re-résolution DNS)."""
    safe_target, error_msg = await resolve_safe_target(target, is_url=False)
    if safe_target is None:
        logger.warning("Sonde TCP refusée pour %r : %s", target, error_msg)
        return {
            "status": "DOWN",
            "latency_ms": None,
            "error": f"{_SSRF_PREFIX}{error_msg}",
            "error_public": _PUBLIC_SSRF_ERROR,
        }

    timeout = max(0.5, min(as_float(timeout, 2.5), 30.0))
    last_error: Optional[BaseException] = None

    for candidate in safe_target.ips[:2]:
        t0 = time.monotonic()
        writer: Optional[asyncio.StreamWriter] = None
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(candidate, safe_target.port), timeout=timeout
            )
            latency = round((time.monotonic() - t0) * 1000, 1)
            return {"status": "UP", "latency_ms": latency, "error": None, "error_public": None}
        except Exception as exc:  # noqa: BLE001 — toute panne réseau => DOWN
            last_error = exc
        finally:
            if writer is not None:
                with contextlib.suppress(Exception):
                    writer.close()
                    await asyncio.wait_for(writer.wait_closed(), timeout=0.5)

    return {
        "status": "DOWN",
        "latency_ms": None,
        "error": f"{type(last_error).__name__}: {last_error}" if last_error else "Connexion impossible.",
        "error_public": _PUBLIC_UNREACHABLE_ERROR,
    }


async def execute_single_monitor(
    monitor: dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """Exécute une sonde déclarée en base, avec coercitions sûres."""
    monitor = monitor or {}
    m_type = str(monitor.get("monitor_type") or "http").strip().lower()
    target = str(monitor.get("target") or "").strip()
    timeout = max(0.5, min(as_float(monitor.get("timeout_seconds"), 3.0), 30.0))

    try:
        if m_type == "http":
            res = await probe_external_http(
                target=target,
                method=monitor.get("method", "GET"),
                expected_status=as_int(monitor.get("expected_status"), 200),
                timeout=timeout,
                client=client,
            )
        elif m_type == "tcp":
            res = await probe_tcp(target=target, timeout=timeout)
        else:
            res = {
                "status": "UNKNOWN",
                "latency_ms": None,
                "status_code": None,
                "error": f"Type de sonde '{m_type}' non supporté.",
                "error_public": f"Type de sonde '{m_type}' non supporté.",
            }
    except Exception as exc:  # aucune sonde ne doit faire échouer la collecte
        logger.warning("Sonde %s (%s) en erreur inattendue : %s", monitor.get("id"), m_type, exc)
        res = {
            "status": "DOWN",
            "latency_ms": None,
            "status_code": None,
            "error": f"{type(exc).__name__}: {exc}",
            "error_public": _PUBLIC_UNREACHABLE_ERROR,
        }

    return {
        "id": monitor.get("id"),
        "tenant_id": monitor.get("tenant_id"),
        "name": monitor.get("name"),
        "type": m_type,
        "target": target,
        **res,
    }


# ---------------------------------------------------------------------------
# Mesures de l'hôte (CPU, RAM, disque, réseau)
# ---------------------------------------------------------------------------

_GB = 1024 ** 3
_MB = 1024 ** 2
HOST_SAMPLE_TTL_SECONDS = 2.0


def _gb(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value) / _GB, 1)


def _parse_iso(value: Any) -> Optional[datetime]:
    """ISO 8601 tolérant (``Z``, nanosecondes Docker) -> datetime UTC, sinon ``None``."""
    text = str(value or "").strip()
    if not text or text.startswith("0001-"):
        return None
    text = text.replace("Z", "+00:00")
    if "." in text:  # Docker renvoie 9 décimales : Python n'en accepte que 6
        head, _, tail = text.partition(".")
        digits = "".join(ch for ch in tail if ch.isdigit())
        suffix = tail[len(digits):]
        text = f"{head}.{digits[:6]}{suffix}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class HostSampler:
    """Instantané de la machine hôte.

    Aucune valeur n'est inventée : une mesure indisponible (psutil absent,
    permission refusée) vaut ``None`` et l'interface affiche « — ».
    """

    def __init__(self, disk_path: Optional[str] = None) -> None:
        self._disk_path = disk_path or (Path(__file__).resolve().anchor or "/")
        self._lock = threading.Lock()
        self._last: Optional[dict[str, Any]] = None
        self._last_at = 0.0
        self._io_prev: Optional[tuple[float, int]] = None
        self._net_prev: Optional[tuple[float, int]] = None
        if psutil is not None:
            with contextlib.suppress(Exception):
                psutil.cpu_percent(interval=None)   # amorce : la 1re lecture vaut 0

    def sample(self, max_age: float = HOST_SAMPLE_TTL_SECONDS) -> dict[str, Any]:
        """Lecture bloquante (psutil) : à exécuter hors event loop."""
        with self._lock:
            now = time.monotonic()
            if self._last is not None and now - self._last_at < max_age:
                return dict(self._last)
            data = self._read(time.time())
            self._last, self._last_at = data, now
            return dict(data)

    # -- Lecture -----------------------------------------------------------
    def _read(self, now: float) -> dict[str, Any]:
        data: dict[str, Any] = {
            "source": "psutil" if psutil is not None else "system",
            "sampled_at": datetime.now(timezone.utc).isoformat(),
            "platform": sys.platform,
            "hostname": socket.gethostname(),
            "cpu_percent": None, "cpu_logical": os.cpu_count() or 1, "cpu_physical": None,
            "load_avg": None,
            "ram_total_gb": None, "ram_used_gb": None, "ram_available_gb": None,
            "ram_cache_gb": None, "ram_percent": None,
            "disk_total_gb": None, "disk_used_gb": None, "disk_free_gb": None, "disk_percent": None,
            "disk_mount": self._disk_path, "disk_fstype": None,
            "disk_io_rate_mb": None, "net_io_rate_mb": None,
            "sockets_count": None, "conns_established": None, "conns_listening": None,
            "net_interfaces_count": None, "process_count": None, "uptime_sec": None,
        }
        try:
            usage = shutil.disk_usage(self._disk_path)
            data.update(
                disk_total_gb=_gb(usage.total), disk_used_gb=_gb(usage.used), disk_free_gb=_gb(usage.free),
                disk_percent=round(usage.used / usage.total * 100, 1) if usage.total else None,
            )
        except OSError as exc:
            logger.debug("Espace disque illisible : %s", exc)

        with contextlib.suppress(OSError, AttributeError):
            load1 = os.getloadavg()[0]
            data["load_avg"] = round(load1, 2)

        if psutil is None:
            self._read_without_psutil(data)
            return data
        self._read_psutil(data, now)
        return data

    def _read_without_psutil(self, data: dict[str, Any]) -> None:
        """Repli Linux (/proc) : RAM, uptime ; CPU estimé par la charge moyenne."""
        if data["load_avg"] is not None:
            data["cpu_percent"] = round(min(100.0, data["load_avg"] / max(1, data["cpu_logical"]) * 100), 1)
        meminfo: dict[str, int] = {}
        with contextlib.suppress(OSError, ValueError):
            for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
                name, _, rest = line.partition(":")
                meminfo[name] = int(rest.strip().split()[0]) * 1024
        if meminfo.get("MemTotal"):
            total, available = meminfo["MemTotal"], meminfo.get("MemAvailable", 0)
            data.update(
                ram_total_gb=_gb(total), ram_available_gb=_gb(available), ram_used_gb=_gb(total - available),
                ram_cache_gb=_gb(meminfo.get("Cached", 0) + meminfo.get("Buffers", 0)),
                ram_percent=round((total - available) / total * 100, 1),
            )
        with contextlib.suppress(OSError, ValueError, IndexError):
            data["uptime_sec"] = int(float(Path("/proc/uptime").read_text(encoding="ascii").split()[0]))

    def _read_psutil(self, data: dict[str, Any], now: float) -> None:
        with contextlib.suppress(Exception):
            data["cpu_percent"] = round(psutil.cpu_percent(interval=None), 1)
            data["cpu_physical"] = psutil.cpu_count(logical=False)
        with contextlib.suppress(Exception):
            vm = psutil.virtual_memory()
            cache = (getattr(vm, "buffers", 0) or 0) + (getattr(vm, "cached", 0) or 0)
            data.update(
                ram_total_gb=_gb(vm.total), ram_available_gb=_gb(vm.available),
                ram_used_gb=_gb(vm.total - vm.available), ram_percent=round(vm.percent, 1),
                ram_cache_gb=_gb(cache) if cache else None,
            )
        with contextlib.suppress(Exception):
            anchor = str(self._disk_path).lower()
            for part in psutil.disk_partitions(all=False):
                if part.mountpoint.lower() == anchor:
                    data["disk_fstype"] = part.fstype
                    break
        with contextlib.suppress(Exception):
            counters = psutil.disk_io_counters()
            if counters:
                total = counters.read_bytes + counters.write_bytes
                data["disk_io_rate_mb"] = self._rate("_io_prev", now, total)
        with contextlib.suppress(Exception):
            counters = psutil.net_io_counters()
            if counters:
                total = counters.bytes_sent + counters.bytes_recv
                data["net_io_rate_mb"] = self._rate("_net_prev", now, total)
        with contextlib.suppress(Exception):
            conns = psutil.net_connections(kind="inet")
            data["sockets_count"] = len(conns)
            data["conns_established"] = sum(1 for c in conns if getattr(c, "status", None) == "ESTABLISHED")
            data["conns_listening"] = sum(1 for c in conns if getattr(c, "status", None) == "LISTEN")
        with contextlib.suppress(Exception):
            data["net_interfaces_count"] = len(psutil.net_if_addrs())
        with contextlib.suppress(Exception):
            data["process_count"] = len(psutil.pids())
        with contextlib.suppress(Exception):
            data["uptime_sec"] = int(now - psutil.boot_time())

    def _rate(self, attr: str, now: float, total_bytes: int) -> Optional[float]:
        previous = getattr(self, attr)
        setattr(self, attr, (now, total_bytes))
        if previous is None:
            return None
        elapsed = now - previous[0]
        if elapsed <= 0.2:
            return None
        return round(max(0.0, (total_bytes - previous[1]) / _MB / elapsed), 2)


class HostHistory:
    """Historique de charge : minutes en mémoire (24 h), heures persistées (Supabase).

    Chaque échantillon alimente la minute courante et l'heure courante ; les heures
    modifiées sont upsertées périodiquement (``host_metrics_hourly``) pour les vues
    7 et 30 jours, qui survivent ainsi aux redémarrages.
    """

    MINUTES_KEPT = 24 * 60
    FIELDS = ("cpu", "ram", "disk_io", "net_io", "fleet_mem")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._minutes: "collections.OrderedDict[int, list[float]]" = collections.OrderedDict()
        self._hours: dict[int, list[float]] = {}
        self._dirty_hours: set[int] = set()
        self._seeded_hours: set[int] = set()

    @staticmethod
    def _vector(sample: dict[str, Any], fleet_mem_mb: float) -> Optional[list[float]]:
        cpu, ram = sample.get("cpu_percent"), sample.get("ram_percent")
        if cpu is None and ram is None:
            return None
        return [
            1.0,
            as_float(cpu, 0.0), as_float(ram, 0.0),
            as_float(sample.get("disk_io_rate_mb"), 0.0), as_float(sample.get("net_io_rate_mb"), 0.0),
            as_float(fleet_mem_mb, 0.0),
        ]

    def add(self, sample: dict[str, Any], fleet_mem_mb: float, now: Optional[float] = None) -> None:
        vector = self._vector(sample, fleet_mem_mb)
        if vector is None:
            return
        now = now or time.time()
        minute, hour = int(now // 60), int(now // 3600)
        with self._lock:
            for store, key in ((self._minutes, minute), (self._hours, hour)):
                acc = store.setdefault(key, [0.0] * len(vector))
                for index, value in enumerate(vector):
                    acc[index] += value
            self._dirty_hours.add(hour)
            while len(self._minutes) > self.MINUTES_KEPT:
                self._minutes.popitem(last=False)
            for stale in [h for h in self._hours if h < hour - 2]:
                self._hours.pop(stale, None)

    # -- Persistance horaire ----------------------------------------------
    def unseeded_dirty_hours(self) -> list[int]:
        with self._lock:
            return sorted(h for h in self._dirty_hours if h not in self._seeded_hours)

    def seed(self, rows: list[dict[str, Any]], hours: Iterable[int]) -> None:
        """Ajoute les agrégats déjà en base (redémarrage en cours d'heure : rien n'est perdu)."""
        wanted = set(hours)
        with self._lock:
            for row in rows:
                stamp = _parse_iso(row.get("hour"))
                if stamp is None:
                    continue
                key = int(stamp.timestamp() // 3600)
                if key not in wanted or key in self._seeded_hours or key not in self._hours:
                    continue
                acc = self._hours[key]
                acc[0] += as_float(row.get("samples"), 0.0)
                for index, field in enumerate(self.FIELDS, start=1):
                    acc[index] += as_float(row.get(f"{field}_sum"), 0.0)
            self._seeded_hours.update(wanted)

    def pending_rows(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = []
            for hour in sorted(self._dirty_hours):
                acc = self._hours.get(hour)
                if not acc:
                    continue
                row = {"hour": datetime.fromtimestamp(hour * 3600, timezone.utc).isoformat(), "samples": int(acc[0])}
                for index, field in enumerate(self.FIELDS, start=1):
                    row[f"{field}_sum"] = round(acc[index], 3)
                rows.append(row)
            return rows

    def mark_flushed(self) -> None:
        with self._lock:
            current = int(time.time() // 3600)
            self._dirty_hours = {h for h in self._dirty_hours if h >= current}

    # -- Lecture ------------------------------------------------------------
    def timeline(self, period: str, hourly_rows: Optional[list[dict[str, Any]]] = None,
                 now: Optional[float] = None) -> dict[str, Any]:
        """Série {labels, series} pour 1h, 24h (mémoire), 7j ou 30j (heures persistées)."""
        now = now or time.time()
        period = {"7j": "7d", "30j": "30d"}.get(period, period)
        if period not in ("1h", "24h", "7d", "30d"):
            period = "24h"

        with self._lock:
            minutes = dict(self._minutes)
            hours = {k: list(v) for k, v in self._hours.items()}

        if period in ("1h", "24h"):
            step, count = (60, 60) if period == "1h" else (900, 96)
            buckets = self._bucketize(minutes, unit=60, step=step, count=count, now=now)
            persisted = False
        else:
            merged: dict[int, list[float]] = {}
            for row in hourly_rows or []:
                stamp = _parse_iso(row.get("hour"))
                if stamp is None:
                    continue
                vector = [as_float(row.get("samples"), 0.0)] + [
                    as_float(row.get(f"{f}_sum"), 0.0) for f in self.FIELDS
                ]
                merged[int(stamp.timestamp() // 3600)] = vector
            merged.update(hours)          # l'heure courante (mémoire) prime sur la base
            step, count = (3600, 168) if period == "7d" else (6 * 3600, 120)
            buckets = self._bucketize(merged, unit=3600, step=step, count=count, now=now)
            persisted = bool(hourly_rows)

        labels = [datetime.fromtimestamp(start, timezone.utc).isoformat() for start, _ in buckets]

        def serie(index: int) -> list[Optional[float]]:
            return [round(acc[index] / acc[0], 2) if acc and acc[0] else None for _, acc in buckets]

        return {
            "period": period,
            "labels": labels,
            "series": [
                {"key": "cpu", "name": "CPU hôte", "unit": "%", "values": serie(1)},
                {"key": "ram", "name": "RAM hôte", "unit": "%", "values": serie(2)},
                {"key": "disk_io", "name": "Débit disque", "unit": "Mo/s", "values": serie(3)},
            ],
            "persisted": persisted,
        }

    @staticmethod
    def _bucketize(source: dict[int, list[float]], *, unit: int, step: int, count: int,
                   now: float) -> list[tuple[int, Optional[list[float]]]]:
        end = int(now // step) * step
        starts = [end - step * i for i in range(count - 1, -1, -1)]
        buckets: dict[int, list[float]] = {}
        for key, acc in source.items():
            start = (key * unit) // step * step
            if start < starts[0]:
                continue
            target = buckets.setdefault(start, [0.0] * len(acc))
            for index, value in enumerate(acc):
                target[index] += value
        return [(start, buckets.get(start)) for start in starts]


# ---------------------------------------------------------------------------
# Trafic des vitrines (lecture incrémentale de tenant_analytics)
# ---------------------------------------------------------------------------

class TrafficAggregator:
    """Pages vues et visiteurs uniques par vitrine et par jour (14 jours glissants).

    Seules les lignes nouvelles (``id`` > dernier lu) sont lues à chaque cycle ;
    les visiteurs « en direct » sont ceux vus depuis moins de 5 minutes.
    """

    WINDOW_DAYS = 14
    LIVE_SECONDS = 300
    #: Plafond effectif d'une réponse PostgREST (max-rows) : une page plus grande serait
    #: tronquée et prise à tort pour la dernière.
    PAGE_SIZE = 1000
    MAX_PAGES_PER_CYCLE = 50
    #: Mémoire bornée face à une collecte publique (route sans authentification) :
    #: au-delà, les visiteurs d'un jour sont comptés sans déduplication (approximation).
    MAX_VISITORS_PER_DAY = 50_000
    MAX_LIVE_PER_TENANT = 10_000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.last_id = 0
        self.loaded = False
        # (tenant, jour) -> [pages vues, {visiteurs}, visiteurs hors plafond]
        self._days: dict[tuple[str, str], list[Any]] = {}
        self._seen: dict[str, dict[str, float]] = {}           # tenant -> {visiteur: epoch}

    def window_start(self, now: Optional[float] = None) -> datetime:
        today = datetime.fromtimestamp(now or time.time(), timezone.utc).date()
        start = today - timedelta(days=self.WINDOW_DAYS - 1)
        return datetime(start.year, start.month, start.day, tzinfo=timezone.utc)

    def ingest(self, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            for row in rows:
                row_id = as_int(row.get("id"), 0)
                if row_id > self.last_id:
                    self.last_id = row_id
                tenant = str(row.get("tenant_id") or "")
                stamp = _parse_iso(row.get("created_at"))
                if not tenant or stamp is None:
                    continue
                bucket = self._days.setdefault((tenant, stamp.date().isoformat()), [0, set(), 0])
                bucket[0] += 1
                visitor = str(row.get("visitor_id") or "")
                if not visitor:
                    continue
                if visitor in bucket[1] or len(bucket[1]) < self.MAX_VISITORS_PER_DAY:
                    bucket[1].add(visitor)
                else:
                    bucket[2] += 1
                epoch = stamp.timestamp()
                seen = self._seen.setdefault(tenant, {})
                if visitor in seen or len(seen) < self.MAX_LIVE_PER_TENANT:
                    if epoch > seen.get(visitor, 0.0):
                        seen[visitor] = epoch

    def prune(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        first_day = self.window_start(now).date().isoformat()
        with self._lock:
            for key in [k for k in self._days if k[1] < first_day]:
                self._days.pop(key, None)
            for tenant, seen in list(self._seen.items()):
                fresh = {v: t for v, t in seen.items() if now - t < self.LIVE_SECONDS}
                if fresh:
                    self._seen[tenant] = fresh
                else:
                    self._seen.pop(tenant, None)

    def forget(self, tenant: str) -> None:
        """Oublie le trafic d'une vitrine purgée."""
        with self._lock:
            for key in [k for k in self._days if k[0] == tenant]:
                self._days.pop(key, None)
            self._seen.pop(tenant, None)

    def series(self, tenant: Optional[str] = None, now: Optional[float] = None) -> list[dict[str, Any]]:
        start = self.window_start(now).date()
        days = [(start + timedelta(days=i)).isoformat() for i in range(self.WINDOW_DAYS)]
        totals = {day: [0, 0] for day in days}
        with self._lock:
            for (row_tenant, day), (pageviews, visitors, overflow) in self._days.items():
                if day in totals and (tenant is None or row_tenant == tenant):
                    totals[day][0] += pageviews
                    totals[day][1] += len(visitors) + overflow
        return [{"date": day, "pageviews": pv, "visitors": uv} for day, (pv, uv) in totals.items()]

    def live(self, tenant: Optional[str] = None, now: Optional[float] = None) -> int:
        now = now or time.time()
        with self._lock:
            groups = [self._seen.get(tenant, {})] if tenant else list(self._seen.values())
            return sum(1 for seen in groups for epoch in seen.values() if now - epoch < self.LIVE_SECONDS)

    def live_by_tenant(self, now: Optional[float] = None) -> dict[str, int]:
        now = now or time.time()
        with self._lock:
            return {
                tenant: count for tenant, seen in self._seen.items()
                if (count := sum(1 for epoch in seen.values() if now - epoch < self.LIVE_SECONDS))
            }


# ---------------------------------------------------------------------------
# Disponibilité par service (agrégats quotidiens, SLA 90 jours)
# ---------------------------------------------------------------------------

UPTIME_UP = "up"
UPTIME_DEGRADED = "degraded"
UPTIME_DOWN = "down"
UPTIME_SKIPPED = "skipped"
UPTIME_DAYS = 90


@dataclass(frozen=True)
class ProbeOutcome:
    """Résultat normalisé d'une sonde, enregistré dans l'historique quotidien."""
    key: str
    label: str
    kind: str
    state: str
    critical: bool = False
    latency_ms: Optional[float] = None
    code: str = ""
    detail: str = ""
    target: str = ""


def _day_state(uptime: Optional[float]) -> str:
    if uptime is None:
        return "none"
    if uptime >= 100.0:
        return UPTIME_UP
    return UPTIME_DEGRADED if uptime >= 99.0 else UPTIME_DOWN


class UptimeRecorder:
    """Agrège les sondes par service et par jour (UTC) ; historique relu en base.

    Un jour est ``up`` à 100 %, ``degraded`` au-delà de 99 %, ``down`` en dessous.
    Un arrêt volontaire (conteneur stoppé) est « hors mesure » : jamais compté en panne.
    """

    COUNTERS = ("checks", "up_count", "degraded_count", "down_count")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._today = ""
        self._rows: dict[str, dict[str, Any]] = {}        # clé -> ligne du jour
        self._skipped: dict[str, int] = {}                # hors mesure (non persisté)
        self._dirty: set[str] = set()
        self._carry: list[dict[str, Any]] = []             # lignes de la veille à persister
        self._seeded: set[str] = set()
        self._meta: dict[str, dict[str, Any]] = {}        # clé -> libellé, type, critique, cible
        self._history: dict[str, dict[str, dict[str, Any]]] = {}   # clé -> jour -> ligne
        self.history_loaded_at = 0.0
        self.persisted = False

    @staticmethod
    def today(now: Optional[float] = None) -> str:
        return datetime.fromtimestamp(now or time.time(), timezone.utc).date().isoformat()

    def record(self, outcomes: Iterable[ProbeOutcome], now: Optional[float] = None) -> None:
        day = self.today(now)
        stamp = datetime.fromtimestamp(now or time.time(), timezone.utc).isoformat()
        with self._lock:
            if day != self._today:
                for key, row in self._rows.items():         # la veille bascule dans l'historique
                    self._history.setdefault(key, {})[row["day"]] = dict(row)
                    if key in self._dirty:                  # derniers compteurs de la veille
                        self._carry.append(dict(row))
                self._today, self._rows, self._skipped, self._seeded = day, {}, {}, set()
                self._dirty = set()
            for outcome in outcomes:
                self._meta[outcome.key] = {
                    "label": outcome.label, "kind": outcome.kind,
                    "critical": outcome.critical, "target": outcome.target,
                }
                row = self._rows.setdefault(outcome.key, {
                    "service_key": outcome.key, "day": day, "label": outcome.label,
                    "kind": outcome.kind, "critical": outcome.critical,
                    "checks": 0, "up_count": 0, "degraded_count": 0, "down_count": 0,
                    "latency_sum_ms": 0.0, "latency_samples": 0,
                    "last_state": outcome.state, "last_code": "", "last_detail": "", "last_checked_at": stamp,
                })
                row.update(label=outcome.label, kind=outcome.kind, critical=outcome.critical,
                           last_state=outcome.state, last_code=outcome.code[:120],
                           last_detail=outcome.detail[:300], last_checked_at=stamp)
                if outcome.state == UPTIME_SKIPPED:
                    self._skipped[outcome.key] = self._skipped.get(outcome.key, 0) + 1
                else:
                    row["checks"] += 1
                    row[f"{outcome.state}_count"] += 1
                    if outcome.latency_ms is not None:
                        row["latency_sum_ms"] += float(outcome.latency_ms)
                        row["latency_samples"] += 1
                self._dirty.add(outcome.key)

    # -- Persistance --------------------------------------------------------
    def unseeded_keys(self) -> list[str]:
        with self._lock:
            return [key for key in self._dirty if key not in self._seeded]

    def seed(self, rows: list[dict[str, Any]], keys: Iterable[str]) -> None:
        """Cumule les compteurs déjà en base pour aujourd'hui (redémarrage de l'API)."""
        wanted = set(keys)
        with self._lock:
            for row in rows:
                key = str(row.get("service_key") or "")
                if key in wanted and key not in self._seeded and key in self._rows and row.get("day") == self._today:
                    current = self._rows[key]
                    for counter in self.COUNTERS + ("latency_samples",):
                        current[counter] += as_int(row.get(counter), 0)
                    current["latency_sum_ms"] += as_float(row.get("latency_sum_ms"), 0.0)
            self._seeded.update(wanted)

    def pending_rows(self) -> list[dict[str, Any]]:
        with self._lock:
            return [*self._carry, *(dict(self._rows[key]) for key in self._dirty if key in self._rows)]

    def mark_flushed(self) -> None:
        with self._lock:
            self._dirty = set()
            self._carry = []
            self.persisted = True

    def load_history(self, rows: list[dict[str, Any]]) -> None:
        history: dict[str, dict[str, dict[str, Any]]] = {}
        for row in rows:
            key, day = str(row.get("service_key") or ""), str(row.get("day") or "")
            if key and day:
                history.setdefault(key, {})[day] = row
                self._meta.setdefault(key, {
                    "label": row.get("label") or key, "kind": row.get("kind") or "http",
                    "critical": bool(row.get("critical")), "target": "",
                })
        with self._lock:
            self._history = history
            self.history_loaded_at = time.monotonic()

    def forget(self, key: str) -> None:
        """Oublie un service (site purgé) : plus d'historique, rien à persister."""
        with self._lock:
            for store in (self._rows, self._skipped, self._meta, self._history):
                store.pop(key, None)
            self._dirty.discard(key)
            self._seeded.discard(key)
            self._carry = [row for row in self._carry if row.get("service_key") != key]

    # -- Lecture ------------------------------------------------------------
    def _days_for(self, key: str, today: str) -> dict[str, dict[str, Any]]:
        days = dict(self._history.get(key, {}))
        if key in self._rows:
            days[today] = self._rows[key]
        return days

    def summary(self, key: str, days: int = UPTIME_DAYS, now: Optional[float] = None) -> dict[str, Any]:
        today = self.today(now)
        start = datetime.fromisoformat(today).date() - timedelta(days=days - 1)
        with self._lock:
            rows = self._days_for(key, today)
            meta = dict(self._meta.get(key, {}))
            current = dict(self._rows.get(key) or {})
            skipped = self._skipped.get(key, 0)
        series, checks, good, down, degraded = [], 0, 0, 0, 0
        latency_sum, latency_samples = 0.0, 0
        for offset in range(days):
            day = (start + timedelta(days=offset)).isoformat()
            row = rows.get(day)
            total = as_int(row.get("checks"), 0) if row else 0
            if not total:
                series.append({"date": day, "state": "none", "uptime": None})
                continue
            up_count = as_int(row.get("up_count"), 0)
            deg_count = as_int(row.get("degraded_count"), 0)
            dn_count = as_int(row.get("down_count"), 0)
            uptime = round((up_count + deg_count) / total * 100, 3)
            series.append({"date": day, "state": _day_state(uptime), "uptime": uptime})
            checks += total
            good += up_count + deg_count
            down += dn_count
            degraded += deg_count
            latency_sum += as_float(row.get("latency_sum_ms"), 0.0)
            latency_samples += as_int(row.get("latency_samples"), 0)
        return {
            "key": key,
            "label": meta.get("label") or key,
            "kind": meta.get("kind") or "http",
            "critical": bool(meta.get("critical")),
            "target": meta.get("target") or "",
            "series": series,
            "uptime": round(good / checks * 100, 3) if checks else None,
            "applicable": checks,
            "down": down,
            "degraded": degraded,
            "skipped": skipped,
            "avg_latency_ms": round(latency_sum / latency_samples, 1) if latency_samples else None,
            "current_state": current.get("last_state") or "none",
            "current_code": current.get("last_code") or "",
            "current_detail": current.get("last_detail") or "",
            "last_checked_at": current.get("last_checked_at"),
        }

    def keys(self, prefix: Optional[str] = None) -> list[str]:
        with self._lock:
            known = set(self._meta) | set(self._rows) | set(self._history)
        return sorted(k for k in known if not prefix or k.startswith(prefix))

    def global_series(self, keys: Iterable[str], days: int = UPTIME_DAYS,
                      now: Optional[float] = None) -> tuple[list[dict[str, Any]], Optional[float]]:
        """SLA global : pour chaque jour, pire état parmi les services donnés."""
        summaries = [self.summary(key, days, now) for key in keys]
        if not summaries:
            return [], None
        rank = {"none": 0, UPTIME_UP: 1, UPTIME_DEGRADED: 2, UPTIME_DOWN: 3}
        merged = []
        for index in range(days):
            points = [s["series"][index] for s in summaries]
            measured = [p for p in points if p["uptime"] is not None]
            worst = max(points, key=lambda p: rank.get(p["state"], 0))
            merged.append({
                "date": points[0]["date"],
                "state": worst["state"],
                "uptime": min(p["uptime"] for p in measured) if measured else None,
            })
        checks = sum(s["applicable"] for s in summaries)
        good = sum((s["uptime"] or 0) * s["applicable"] / 100 for s in summaries if s["uptime"] is not None)
        return merged, (round(good / checks * 100, 3) if checks else None)


# ---------------------------------------------------------------------------
# Inventaire des conteneurs (lecture seule des attributs Docker)
# ---------------------------------------------------------------------------

CONTAINER_SIZE_TTL_SECONDS = 600.0

#: État Docker -> statut affiché par le cockpit.
_DOCKER_STATUS = {
    "running": "running", "restarting": "restarting", "paused": "paused",
    "created": "starting", "removing": "stopping", "exited": "stopped", "dead": "error",
}


def _tenant_of(container: Any) -> str:
    labels = getattr(container, "labels", None) or {}
    return str(labels.get("tenant.id") or str(getattr(container, "name", "")).lstrip("/").replace("waas-", "", 1))


def container_host_port(attrs: dict[str, Any]) -> Optional[str]:
    ports = ((attrs or {}).get("NetworkSettings") or {}).get("Ports") or {}
    for key in WEB_PORT_KEYS:
        mapping = ports.get(key) or []
        if mapping and isinstance(mapping[0], dict) and mapping[0].get("HostPort"):
            return str(mapping[0]["HostPort"])
    return None


def describe_container(container: Any, sizes: Optional[dict[str, float]] = None) -> dict[str, Any]:
    """Fiche d'exploitation d'un conteneur locataire (sans aucun secret de l'environnement)."""
    attrs = getattr(container, "attrs", None) or {}
    state = attrs.get("State") or {}
    config = attrs.get("Config") or {}
    raw_status = str(state.get("Status") or getattr(container, "status", "") or "unknown").lower()
    status = _DOCKER_STATUS.get(raw_status, raw_status)
    exit_code = as_int(state.get("ExitCode"), 0)
    if status == "stopped" and exit_code not in (0, 137, 143):   # 137/143 : arrêt demandé
        status = "error"
    started = _parse_iso(state.get("StartedAt")) if status == "running" else None
    env = {
        entry.split("=", 1)[0]: entry.split("=", 1)[1]
        for entry in (config.get("Env") or []) if isinstance(entry, str) and "=" in entry
    }
    short_id = str(getattr(container, "short_id", "") or str(attrs.get("Id", ""))[:12])
    memory_limit = as_int((attrs.get("HostConfig") or {}).get("Memory"), 0)
    return {
        "site_id": _tenant_of(container),
        "name": str(getattr(container, "name", "")).lstrip("/"),
        "container_id": short_id,
        "status": status,
        "docker_status": raw_status,
        "running": status == "running",
        "health": (state.get("Health") or {}).get("Status"),
        "exit_code": exit_code,
        "restart_count": as_int(attrs.get("RestartCount"), 0),
        "started_at": started.isoformat() if started else None,
        "started_at_epoch": int(started.timestamp()) if started else None,
        "created_at": attrs.get("Created"),
        "image": config.get("Image"),
        # REPO_URL est public par construction (build_tenant_environment) ; SYNC_TOKEN n'est jamais exposé.
        "repo_url": env.get("REPO_URL"),
        "port": container_host_port(attrs),
        "disk_mb": (sizes or {}).get(short_id[:12]),
        "memory_limit_mb": round(memory_limit / _MB) if memory_limit > 0 else None,
    }


def _metric_fields(metric: Optional[dict[str, Any]]) -> dict[str, Any]:
    metric = metric or {}
    return {
        "cpu_percent": metric.get("cpu_percent"),
        "memory_mb": metric.get("memory_mb"),
        "pids": metric.get("pids"),
    }


def _probe_code(result: dict[str, Any]) -> str:
    code = result.get("status_code")
    if code:
        return f"HTTP {code}"
    return {"UP": "OK", "DOWN": "Injoignable", "DEGRADED": "Réponse inattendue"}.get(
        str(result.get("status") or "").upper(), "—"
    )


# ---------------------------------------------------------------------------
# Moteur de collecte
# ---------------------------------------------------------------------------

class TelemetryEngine:
    """Worker de télémétrie : collecte périodique, état en mémoire, O(1) en lecture."""

    def __init__(self) -> None:
        self._docker_client: Optional[docker.DockerClient] = None
        self._docker_client_lock = asyncio.Lock()
        self._is_running = False
        self._task: Optional[asyncio.Task] = None
        self._http_client: Optional[httpx.AsyncClient] = None
        self._http_transport: Optional[DynamicPinnedIPTransport] = None

        # Exécuteurs dédiés : l'exécuteur par défaut d'asyncio n'est jamais saturé
        # par des appels Docker/Supabase bloquants.
        self._docker_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="waas-docker"
        )
        self._db_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="waas-db"
        )
        self._docker_semaphore = asyncio.Semaphore(8)
        self._abandoned_threads = 0

        self._last_user_activity: float = time.time()
        self._active_window_seconds = 600.0
        self._active_interval_seconds = 15.0
        self._idle_interval_seconds = 45.0

        self._stats_data: dict[str, Any] = {
            "active_tenants": 0,
            "total_memory_usage_mb": 0.0,
            "tenants": [],
            "cached_at": None,
            "stale": True,
        }

        self._health_data: dict[str, Any] = {
            "status": "UNKNOWN",
            "probe_duration_ms": 0.0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cached_at": None,
            "stale": True,
            "infrastructure": {
                "api": {"status": "UP"},
                "database": {"status": "UNKNOWN", "latency_ms": None},
                "docker_engine": {"status": "UNKNOWN"},
            },
            "fleet": {
                "total_tenants": 0,
                "running": 0,
                "restarting_crashloop": 0,
                "stopped": 0,
                "http_unreachable": 0,
                "alerts": [],
                "probes_skipped": 0,
            },
            "external_services": {
                "total_configured": 0,
                "operational": 0,
                "failing": [],
                "truncated": False,
            },
        }

        self._business_data: dict[str, Any] = {
            "total_clients": 0,
            "total_sites": 0,
            "mrr": 0.0,
            "arr": 0.0,
            "currency": "EUR",
            "cached_at": None,
            "stale": True,
        }

        self._last_collected_at: float = 0.0
        self._collection_lock = asyncio.Lock()
        self._collected: dict[str, float] = {"stats": 0.0, "health": 0.0, "business": 0.0}
        self._last_errors: dict[str, str] = {}

        # Supervision étendue (cockpit) : hôte, trafic, disponibilité, journal des sondes.
        self.host = HostSampler()
        self.host_history = HostHistory()
        self.traffic = TrafficAggregator()
        self.uptime = UptimeRecorder()
        self._host_data: dict[str, Any] = {}
        self._probe_log: "collections.deque[dict[str, Any]]" = collections.deque(maxlen=60)
        self._container_sizes: dict[str, float] = {}
        self._sizes_at = 0.0
        self._timers: dict[str, float] = {"uptime_flush": 0.0, "host_flush": 0.0, "uptime_history": 0.0}

    # -- Infrastructure ----------------------------------------------------
    @property
    def http_client(self) -> Optional[httpx.AsyncClient]:
        """Client partagé (None tant que le moteur n'est pas démarré)."""
        return self._http_client

    def is_running(self) -> bool:
        return self._is_running

    def _ensure_executors(self) -> None:
        if self._docker_executor._shutdown:
            self._docker_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=8, thread_name_prefix="waas-docker"
            )
        if self._db_executor._shutdown:
            self._db_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="waas-db"
            )

    async def _run_blocking(
        self,
        func: Callable[..., Any],
        *args: Any,
        timeout: float = 10.0,
        executor: Optional[concurrent.futures.Executor] = None,
        on_timeout: str = "raise",
        **kwargs: Any,
    ) -> Any:
        """Exécute un appel bloquant hors de l'event loop, avec échéance."""
        loop = asyncio.get_running_loop()
        call = functools.partial(func, *args, **kwargs)
        future = loop.run_in_executor(executor, call)
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except asyncio.TimeoutError:
            self._abandoned_threads += 1
            name = getattr(func, "__name__", repr(func))
            logger.warning(
                "Échéance de %.1fs dépassée sur %s : thread non annulable abandonné "
                "(%d au total)", timeout, name, self._abandoned_threads,
            )
            if on_timeout == "none":
                return None
            if on_timeout == "previous":
                return None
            raise

    async def get_docker_client_async(self) -> Optional[docker.DockerClient]:
        """Client Docker créé hors event loop (``docker.from_env()`` fait un GET /version)."""
        if self._docker_client is not None:
            return self._docker_client
        async with self._docker_client_lock:
            if self._docker_client is not None:
                return self._docker_client
            try:
                self._docker_client = await self._run_blocking(
                    docker.from_env, timeout=10.0, executor=self._docker_executor,
                    on_timeout="none",
                )
            except Exception as exc:
                logger.warning("Connexion Docker impossible : %s", exc)
                self._docker_client = None
        return self._docker_client

    def get_docker_client(self) -> Optional[docker.DockerClient]:
        """Accès synchrone (hors event loop uniquement) — conservé pour compatibilité."""
        if self._docker_client is None:
            try:
                self._docker_client = docker.from_env(timeout=10)
            except Exception as exc:
                logger.warning("Connexion Docker impossible : %s", exc)
                self._docker_client = None
        return self._docker_client

    def invalidate_docker_client(self) -> None:
        """Force la reconnexion au prochain cycle (démon redémarré, socket changée…)."""
        self._docker_client = None

    # -- Activité ----------------------------------------------------------
    def touch_activity(self) -> None:
        self._last_user_activity = time.time()

    def is_active_mode(self) -> bool:
        return (time.time() - self._last_user_activity) < self._active_window_seconds

    # -- Collecte : ressources Docker --------------------------------------
    async def _collect_docker_stats(self) -> dict[str, Any]:
        client = await self.get_docker_client_async()
        if not client:
            return self._stale_stats("Daemon Docker indisponible.")

        try:
            all_containers = await self._run_blocking(
                client.containers.list,
                timeout=8.0,
                executor=self._docker_executor,
                all=True,
                filters={"label": "service=waas-tenant"},
            ) or []
        except Exception as exc:
            logger.error("Listing des conteneurs impossible : %s", exc)
            return self._stale_stats(f"Listing conteneurs impossible : {type(exc).__name__}")
        containers = [c for c in all_containers if str(getattr(c, "status", "")).lower() == "running"]
        await self._refresh_container_sizes(client)

        stats_timeouts = 0

        def fetch_single_container_metric(c: Any) -> Optional[dict[str, Any]]:
            tenant_id = (getattr(c, "labels", None) or {}).get(
                "tenant.id", str(c.name).replace("waas-", "")
            )
            try:
                if str(c.status).lower() != "running":
                    return None
                stats = c.stats(stream=False)
                memory_stats = stats.get("memory_stats") or {}
                memory_usage = memory_stats.get("usage")
                if memory_usage is None:
                    logger.warning("stats sans memory_stats.usage pour %s", tenant_id)
                    memory_usage = 0
                return {
                    "site_id": tenant_id,
                    "tenant_id": tenant_id,
                    "cpu_percent": calculate_cpu_percent(stats),
                    "memory_mb": round(as_float(memory_usage, 0.0) / (1024 * 1024), 2),
                    "pids": as_int((stats.get("pids_stats") or {}).get("current"), 0),
                }
            except (NotFound, APIError) as exc:
                logger.warning("stats indisponibles pour %s : %s", tenant_id, exc)
                return None
            except Exception:
                logger.exception("stats inattendues pour %s", tenant_id)
                return None

        async def bounded_fetch(c: Any) -> Optional[dict[str, Any]]:
            nonlocal stats_timeouts
            async with self._docker_semaphore:
                result = await self._run_blocking(
                    fetch_single_container_metric, c,
                    timeout=6.0, executor=self._docker_executor, on_timeout="none",
                )
                if result is None and str(getattr(c, "status", "")).lower() == "running":
                    stats_timeouts += 1
                return result

        fleet_metrics: list[dict[str, Any]] = []
        if containers:
            results = await asyncio.gather(
                *(bounded_fetch(c) for c in containers), return_exceptions=True
            )
            fleet_metrics = [r for r in results if isinstance(r, dict)]
            failures = [r for r in results if isinstance(r, BaseException)]
            if failures:
                logger.warning("%d conteneurs sans métrique exploitable", len(failures))

        total_mem = round(sum(as_float(m.get("memory_mb"), 0.0) for m in fleet_metrics), 2)
        metrics_by_id = {m["site_id"]: m for m in fleet_metrics}
        inventory = [
            {**describe_container(c, self._container_sizes), **_metric_fields(metrics_by_id.get(_tenant_of(c)))}
            for c in all_containers
        ]
        self._collected["stats"] = time.time()
        return {
            "active_tenants": len(fleet_metrics),
            "total_memory_usage_mb": total_mem,
            "total_disk_mb": round(sum(as_float(i.get("disk_mb"), 0.0) for i in inventory), 1),
            "total_containers": len(all_containers),
            "tenants": fleet_metrics,
            "containers": inventory,
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "stale": False,
            "stats_timeouts": stats_timeouts,
        }

    async def _refresh_container_sizes(self, client: Any) -> None:
        """Taille disque (couche en écriture) des conteneurs : coûteuse, relue toutes les 10 min."""
        if time.monotonic() - self._sizes_at < CONTAINER_SIZE_TTL_SECONDS:
            return
        self._sizes_at = time.monotonic()
        try:
            rows = await self._run_blocking(
                client.api.containers, timeout=20.0, executor=self._docker_executor, on_timeout="none",
                all=True, size=True, filters={"label": "service=waas-tenant"},
            ) or []
        except Exception as exc:
            logger.debug("Tailles des conteneurs indisponibles : %s", exc)
            return
        self._container_sizes = {
            str(row.get("Id", ""))[:12]: round(as_float(row.get("SizeRw"), 0.0) / _MB, 1)
            for row in rows if isinstance(row, dict)
        }

    def _stale_stats(self, reason: str) -> dict[str, Any]:
        previous = _snapshot(self._stats_data)
        previous["stale"] = True
        previous["error"] = reason
        return previous

    # -- Collecte : santé --------------------------------------------------
    async def _collect_health_status(self) -> dict[str, Any]:
        t_start = time.monotonic()
        policy = get_policy()

        db_check: dict[str, Any] = {"status": "DOWN", "latency_ms": None}
        t0 = time.monotonic()
        try:
            await self._run_blocking(
                lambda: db.supabase.table("fleet_settings").select("key").limit(1).execute(),
                timeout=8.0, executor=self._db_executor,
            )
            db_check["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            db_check["status"] = "UP"
        except Exception as exc:
            db_check["status"] = "DOWN"
            db_check["error"] = f"{type(exc).__name__}: {exc}"

        client = await self.get_docker_client_async()
        docker_check: dict[str, Any] = {"status": "DOWN"}
        containers: list[Any] = []
        if client:
            try:
                await self._run_blocking(
                    client.ping, timeout=5.0, executor=self._docker_executor
                )
                docker_check["status"] = "UP"
                containers = await self._run_blocking(
                    client.containers.list,
                    timeout=10.0,
                    executor=self._docker_executor,
                    all=True,
                    filters={"label": "service=waas-tenant"},
                ) or []
            except Exception as exc:
                docker_check["status"] = "DOWN"
                docker_check["error"] = f"{type(exc).__name__}: {exc}"
                self.invalidate_docker_client()

        fleet_summary: dict[str, Any] = {
            "total_tenants": len(containers),
            "running": 0,
            "restarting_crashloop": 0,
            "stopped": 0,
            "http_unreachable": 0,
            "alerts": [],
            "probes_skipped": 0,
        }

        async def probe_container(c: Any) -> dict[str, Any]:
            labels = getattr(c, "labels", None) or {}
            t_id = labels.get("tenant.id", str(c.name).replace("waas-", ""))
            c_status = str(c.status).lower()
            ports = ((getattr(c, "attrs", None) or {}).get("NetworkSettings") or {}).get("Ports") or {}
            host_port = None
            for port_key in WEB_PORT_KEYS:          # 8080 (image durcie) ou 80 (historique)
                mapping = ports.get(port_key) or []
                if mapping and isinstance(mapping[0], dict) and mapping[0].get("HostPort"):
                    host_port = str(mapping[0]["HostPort"])
                    break

            res: dict[str, Any] = {
                "tenant_id": t_id,
                "container_status": c_status,
                "http_status": "DOWN",
                "latency_ms": None,
                "host_port": host_port,
            }

            if c_status == "running" and host_port:
                http_res = await probe_local_http(host_port, client=self._http_client, timeout=1.5)
                res["http_status"] = http_res["status"]
                res["latency_ms"] = http_res["latency_ms"]
                res["status_code"] = http_res.get("status_code")
                res["error"] = http_res.get("error_public")
            return res

        container_results: list[dict[str, Any]] = []
        if containers:
            container_sem = asyncio.Semaphore(policy.container_probe_concurrency)

            async def bounded_probe(c: Any) -> dict[str, Any]:
                async with container_sem:
                    return await probe_container(c)

            results = await asyncio.gather(
                *(bounded_probe(c) for c in containers), return_exceptions=True
            )
            container_results = [r for r in results if isinstance(r, dict)]
            skipped = len(results) - len(container_results)
            fleet_summary["probes_skipped"] = skipped
            if skipped:
                logger.warning("%d conteneurs non sondés (exception locale)", skipped)

        for cr in container_results:
            st = cr["container_status"]
            if st == "running":
                if cr["http_status"] == "UP":
                    fleet_summary["running"] += 1
                else:
                    fleet_summary["http_unreachable"] += 1
                    fleet_summary["alerts"].append({
                        "tenant_id": cr["tenant_id"],
                        "level": "WARNING",
                        "issue": f"Conteneur actif mais le service HTTP ne répond pas (port {cr['host_port']})",
                    })
            elif st == "restarting":
                fleet_summary["restarting_crashloop"] += 1
                fleet_summary["alerts"].append({
                    "tenant_id": cr["tenant_id"],
                    "level": "CRITICAL",
                    "issue": "Crashloop détecté (vérifier les logs du conteneur)",
                })
            else:
                fleet_summary["stopped"] += 1
                fleet_summary["alerts"].append({
                    "tenant_id": cr["tenant_id"],
                    "level": "INFO",
                    "issue": f"Conteneur arrêté ({st})",
                })

        external_results: list[dict[str, Any]] = []
        monitors: list[dict[str, Any]] = []
        try:
            raw_monitors = await self._run_blocking(
                db.get_all_active_monitors, timeout=10.0, executor=self._db_executor
            ) or []
            monitors = [m for m in raw_monitors if isinstance(m, dict)]
            if len(monitors) != len(raw_monitors):
                logger.warning(
                    "%d sondes ignorées (enregistrements illisibles)",
                    len(raw_monitors) - len(monitors),
                )
        except Exception as exc:
            logger.error("Listing des sondes impossible : %s", exc)

        truncated = False
        if policy.max_monitors_per_collection and len(monitors) > policy.max_monitors_per_collection:
            logger.warning(
                "%d sondes actives > plafond %d : les suivantes sont ignorées ce cycle",
                len(monitors), policy.max_monitors_per_collection,
            )
            monitors = monitors[: policy.max_monitors_per_collection]
            truncated = True

        if monitors:
            sem = asyncio.Semaphore(policy.probe_concurrency)

            async def sem_run(m: dict[str, Any]) -> dict[str, Any]:
                async with sem:
                    return await execute_single_monitor(m, client=self._http_client)

            raw = await asyncio.gather(*(sem_run(m) for m in monitors), return_exceptions=True)
            external_results = [r for r in raw if isinstance(r, dict)]
            for failure in (r for r in raw if isinstance(r, BaseException)):
                logger.warning("Sonde non exécutée : %s", failure)

        failing = [
            {
                "tenant_id": r.get("tenant_id"),
                "name": r.get("name"),
                "type": r.get("type"),
                "target": r.get("target"),
                "error": r.get("error"),
            }
            for r in external_results
            if r.get("status") != "UP"
        ]

        external_summary = {
            "total_configured": len(monitors),
            "operational": sum(1 for r in external_results if r.get("status") == "UP"),
            "failing": failing,
            "truncated": truncated,
            # Projection publique (jamais le motif technique interne `error`).
            "results": [
                {
                    "id": r.get("id"), "tenant_id": r.get("tenant_id"), "name": r.get("name"),
                    "type": r.get("type"), "target": r.get("target"), "status": r.get("status"),
                    "latency_ms": r.get("latency_ms"), "status_code": r.get("status_code"),
                    "error": r.get("error_public"),
                }
                for r in external_results
            ],
        }
        fleet_summary["containers"] = container_results

        infra_ok = db_check["status"] == "UP" and docker_check["status"] == "UP"
        has_critical = (
            fleet_summary["restarting_crashloop"] > 0 or fleet_summary["http_unreachable"] > 0
        )
        has_failing_externals = len(failing) > 0

        if not infra_ok or has_critical:
            global_status = "CRITICAL"
        elif has_failing_externals or fleet_summary["stopped"] > 0:
            global_status = "DEGRADED"
        elif fleet_summary["probes_skipped"] > 0:
            global_status = "DEGRADED"
        else:
            global_status = "HEALTHY"

        self._collected["health"] = time.time()
        return {
            "status": global_status,
            "probe_duration_ms": round((time.monotonic() - t_start) * 1000, 1),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "stale": False,
            "infrastructure": {
                "api": {"status": "UP"},
                "database": db_check,
                "docker_engine": docker_check,
            },
            "fleet": fleet_summary,
            "external_services": external_summary,
        }

    # -- Collecte : KPIs métier -------------------------------------------
    async def _collect_business_metrics(self) -> dict[str, Any]:
        try:
            default_price = as_float(
                await self._run_blocking(
                    db.get_setting, "monthly_price_per_site", 199.0,
                    timeout=8.0, executor=self._db_executor,
                ),
                199.0,
            )
            clients = await self._run_blocking(
                db.get_all_clients, timeout=25.0, executor=self._db_executor
            ) or []
        except Exception as exc:
            logger.warning("Erreur collecte KPIs business : %s", exc)
            return self._stale_business(f"{type(exc).__name__}")

        total_clients = 0
        total_sites = 0
        calculated_mrr = 0.0
        seen_sites: set[str] = set()
        anomalies = 0

        for c in clients:
            try:
                if not isinstance(c, dict):
                    continue
                total_clients += 1
                raw_sites = c.get("sites")
                sites = raw_sites if isinstance(raw_sites, (list, tuple)) else []
                if raw_sites and not sites:
                    anomalies += 1
                    logger.warning(
                        "Client %s : champ 'sites' de type %s ignoré",
                        c.get("id"), type(raw_sites).__name__,
                    )
                declared = as_int(c.get("sites_count"), 0)

                if sites and declared and declared != len(sites):
                    anomalies += 1
                    logger.warning(
                        "Incohérence sites_count=%s / sites=%s pour le client %s",
                        declared, len(sites), c.get("id"),
                    )

                if sites:
                    for s in sites:
                        if not isinstance(s, dict):
                            continue
                        key = str(s.get("site_id") or s.get("id") or f"{c.get('id')}:{id(s)}")
                        if key in seen_sites:
                            continue
                        seen_sites.add(key)
                        total_sites += 1
                        # Fiche `sites` (price_monthly) prioritaire, puis anciens champs.
                        price = next(
                            (s.get(k) for k in ("price_monthly", "plan_price") if s.get(k) is not None),
                            c.get("plan_price"),
                        )
                        calculated_mrr += as_float(price, default_price)
                elif declared > 0:
                    key = str(c.get("id") or f"client:{id(c)}")
                    if key not in seen_sites:
                        seen_sites.add(key)
                        total_sites += declared
                        calculated_mrr += declared * as_float(c.get("plan_price"), default_price)
            except Exception as exc:  # un client corrompu n'annule plus la flotte
                anomalies += 1
                logger.warning("Client %s ignoré (données inexploitables) : %s", c.get("id"), exc)

        mrr = round(calculated_mrr, 2)
        self._collected["business"] = time.time()
        return {
            "total_clients": total_clients,
            "total_sites": total_sites,
            "mrr": mrr,
            "arr": round(mrr * 12, 2),
            "currency": "EUR",
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "stale": False,
            "anomalies": anomalies,
        }

    def _stale_business(self, reason: str) -> dict[str, Any]:
        previous = _snapshot(self._business_data)
        previous["stale"] = True
        previous["error"] = reason
        return previous

    # -- Orchestration -----------------------------------------------------
    async def collect_all(self) -> None:
        async with self._collection_lock:
            if _policy_is_stale():
                try:
                    await self._run_blocking(
                        load_probe_policy_sync, timeout=8.0, executor=self._db_executor,
                        on_timeout="none",
                    )
                except Exception as exc:
                    logger.warning("Politique de sondage non rechargée : %s", exc)

            core = asyncio.gather(
                self._collect_docker_stats(),
                self._collect_health_status(),
                self._collect_business_metrics(),
                return_exceptions=True,
            )
            # Collecteurs du cockpit, indépendants : leur échec n'affecte jamais le cœur.
            extras = asyncio.gather(self._collect_host(), self._collect_traffic(), return_exceptions=True)
            (stats_res, health_res, business_res), extra_results = await asyncio.gather(core, extras)
            for failure in (r for r in extra_results if isinstance(r, BaseException)):
                logger.warning("Collecteur secondaire en échec : %r", failure)

            if isinstance(stats_res, dict):
                self._stats_data = stats_res
            else:
                self._stats_data = self._stale_stats(f"{type(stats_res).__name__}")
                self._last_errors["stats"] = str(stats_res)

            if isinstance(health_res, dict):
                self._health_data = health_res
            else:
                self._last_errors["health"] = str(health_res)

            self._business_data = (
                business_res if isinstance(business_res, dict)
                else self._stale_business(f"{type(business_res).__name__}")
            )

            self._last_collected_at = time.time()
            failures = [
                r for r in (stats_res, health_res, business_res) if isinstance(r, BaseException)
            ]
            if failures:
                logger.error("Collecte partielle : %s", [repr(f) for f in failures])

            if isinstance(health_res, dict):
                try:
                    self.record_probe_results(health_res)
                    await self._persist_supervision()
                except Exception:
                    logger.exception("Historique de disponibilité non mis à jour")

    # -- Collecte : hôte, trafic, disponibilité (cockpit) -------------------
    async def _collect_host(self) -> dict[str, Any]:
        sample = await self._run_blocking(self.host.sample, 0.0, timeout=8.0, on_timeout="none")
        if not isinstance(sample, dict):
            return self._host_data
        fleet_mem = as_float((self._stats_data or {}).get("total_memory_usage_mb"), 0.0)
        self.host_history.add(sample, fleet_mem)
        self._host_data = sample
        return sample

    async def _collect_traffic(self) -> None:
        """Lit les nouveaux hits (id > dernier lu) ; premier passage : 14 jours."""
        since = self.traffic.window_start().isoformat()
        for _ in range(self.traffic.MAX_PAGES_PER_CYCLE):
            rows = await self._run_blocking(
                db.fetch_hits_after, self.traffic.last_id, since, self.traffic.PAGE_SIZE,
                timeout=15.0, executor=self._db_executor,
            ) or []
            self.traffic.ingest(rows)
            if len(rows) < self.traffic.PAGE_SIZE:
                break
        self.traffic.loaded = True
        self.traffic.prune()

    def record_probe_results(self, health: dict[str, Any], site_id: Optional[str] = None) -> list[ProbeOutcome]:
        """Normalise un passage de sondes, l'ajoute à l'historique et au journal.

        ``site_id`` limite l'enregistrement aux sondes d'une vitrine (contrôle à la demande).
        """
        outcomes: list[ProbeOutcome] = []
        infra = health.get("infrastructure") or {}
        if site_id is None:
            for key, label, check in (
                ("core:api", "API WaaS", infra.get("api") or {"status": "UP"}),
                ("core:database", "Base Supabase", infra.get("database") or {}),
                ("core:docker", "Moteur Docker", infra.get("docker_engine") or {}),
            ):
                status = str(check.get("status") or "DOWN").upper()
                outcomes.append(ProbeOutcome(
                    key=key, label=label, kind="core", critical=True,
                    state=UPTIME_UP if status == "UP" else UPTIME_DOWN,
                    latency_ms=check.get("latency_ms"),
                    code="OK" if status == "UP" else "Indisponible",
                    detail="" if status == "UP" else "Service injoignable depuis l'API.",
                ))

        for item in (health.get("fleet") or {}).get("containers") or []:
            tenant = str(item.get("tenant_id") or "")
            if not tenant or (site_id and tenant != site_id):
                continue
            c_status = str(item.get("container_status") or "")
            http = str(item.get("http_status") or "DOWN").upper()
            if c_status == "running":
                state = {"UP": UPTIME_UP, "DEGRADED": UPTIME_DEGRADED}.get(http, UPTIME_DOWN)
                detail = "" if state == UPTIME_UP else (item.get("error") or "La page d'accueil ne répond pas.")
            elif c_status == "restarting":
                state, detail = UPTIME_DOWN, "Crashloop : le conteneur redémarre en boucle."
            else:
                state, detail = UPTIME_SKIPPED, "Arrêt volontaire : non mesuré."
            outcomes.append(ProbeOutcome(
                key=f"site:{tenant}", label=f"Vitrine {tenant}", kind="site", state=state,
                latency_ms=item.get("latency_ms") if c_status == "running" else None,
                code=_probe_code({"status": http, "status_code": item.get("status_code")})
                if c_status == "running" else c_status or "—",
                detail=detail, target=f"127.0.0.1:{item.get('host_port')}" if item.get("host_port") else "",
            ))

        for result in (health.get("external_services") or {}).get("results") or []:
            tenant = str(result.get("tenant_id") or "")
            if site_id and tenant != site_id:
                continue
            status = str(result.get("status") or "DOWN").upper()
            outcomes.append(ProbeOutcome(
                key=f"monitor:{result.get('id')}", label=str(result.get("name") or result.get("target") or "Sonde"),
                kind=str(result.get("type") or "http"),
                state={"UP": UPTIME_UP, "DEGRADED": UPTIME_DEGRADED}.get(status, UPTIME_DOWN),
                latency_ms=result.get("latency_ms"), code=_probe_code(result),
                detail=str(result.get("error") or ""), target=str(result.get("target") or ""),
            ))

        self.uptime.record(outcomes)
        stamp = datetime.now(timezone.utc).isoformat()
        for outcome in outcomes:
            if outcome.state == UPTIME_SKIPPED:
                continue
            self._probe_log.appendleft({
                "at": stamp, "key": outcome.key, "label": outcome.label, "kind": outcome.kind,
                "target": outcome.target, "state": outcome.state, "latency_ms": outcome.latency_ms,
                "code": outcome.code, "detail": outcome.detail,
            })
        return outcomes

    async def _persist_supervision(self) -> None:
        """Écritures périodiques (meilleur effort) : disponibilité 60 s, charge horaire 5 min."""
        now = time.monotonic()
        if now - self._timers["uptime_history"] > 600:
            self._timers["uptime_history"] = now
            since = (datetime.now(timezone.utc).date() - timedelta(days=UPTIME_DAYS)).isoformat()
            rows = await self._run_blocking(db.get_uptime_rows, since, timeout=15.0,
                                            executor=self._db_executor, on_timeout="none")
            if isinstance(rows, list):
                self.uptime.load_history(rows)

        if now - self._timers["uptime_flush"] > 60:
            self._timers["uptime_flush"] = now
            keys = self.uptime.unseeded_keys()
            if keys:
                today_rows = await self._run_blocking(db.get_uptime_rows, self.uptime.today(), timeout=10.0,
                                                      executor=self._db_executor, on_timeout="none")
                self.uptime.seed(today_rows if isinstance(today_rows, list) else [], keys)
            saved = await self._run_blocking(db.upsert_uptime_rows, self.uptime.pending_rows(),
                                             timeout=15.0, executor=self._db_executor, on_timeout="none")
            if saved:
                self.uptime.mark_flushed()

        if now - self._timers["host_flush"] > 300:
            self._timers["host_flush"] = now
            hours = self.host_history.unseeded_dirty_hours()
            if hours:
                since = datetime.fromtimestamp(hours[0] * 3600, timezone.utc).isoformat()
                existing = await self._run_blocking(db.get_host_hourly, since, timeout=10.0,
                                                    executor=self._db_executor, on_timeout="none")
                self.host_history.seed(existing if isinstance(existing, list) else [], hours)
            saved = await self._run_blocking(db.upsert_host_hourly, self.host_history.pending_rows(),
                                             timeout=15.0, executor=self._db_executor, on_timeout="none")
            if saved:
                self.host_history.mark_flushed()

    async def probe_site_now(self, site_id: str) -> dict[str, Any]:
        """Sondes immédiates d'une vitrine (page d'accueil + sondes externes du site)."""
        client = await self.get_docker_client_async()
        containers: list[dict[str, Any]] = []
        if client:
            try:
                container = await self._run_blocking(
                    client.containers.get, f"waas-{site_id}", timeout=6.0, executor=self._docker_executor
                )
                status = str(container.status).lower()
                port = container_host_port(container.attrs)
                item: dict[str, Any] = {"tenant_id": site_id, "container_status": status, "host_port": port,
                                        "http_status": "DOWN", "latency_ms": None}
                if status == "running" and port:
                    http = await probe_local_http(port, client=self._http_client, timeout=2.0)
                    item.update(http_status=http["status"], latency_ms=http["latency_ms"],
                                status_code=http.get("status_code"), error=http.get("error_public"))
                containers.append(item)
            except NotFound:
                pass
            except Exception as exc:
                logger.warning("Sonde immédiate de %s impossible : %s", site_id, exc)

        monitors = await self._run_blocking(db.get_tenant_monitors, site_id, timeout=8.0,
                                            executor=self._db_executor, on_timeout="none") or []
        results = await asyncio.gather(
            *(execute_single_monitor(m, client=self._http_client) for m in monitors if isinstance(m, dict)),
            return_exceptions=True,
        )
        external = [
            {"id": r.get("id"), "tenant_id": r.get("tenant_id"), "name": r.get("name"), "type": r.get("type"),
             "target": r.get("target"), "status": r.get("status"), "latency_ms": r.get("latency_ms"),
             "status_code": r.get("status_code"), "error": r.get("error_public")}
            for r in results if isinstance(r, dict)
        ]
        outcomes = self.record_probe_results(
            {"fleet": {"containers": containers}, "external_services": {"results": external}}, site_id=site_id
        )
        return {"site_id": site_id, "probed": len(outcomes), "external": external, "containers": containers}

    async def _worker_loop(self) -> None:
        logger.info("Moteur de télémétrie démarré (single-worker, état en mémoire).")
        await self.collect_all()

        while self._is_running:
            interval = self._active_interval_seconds if self.is_active_mode() else self._idle_interval_seconds
            t0 = time.monotonic()
            try:
                await self.collect_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Exception dans la boucle de télémétrie")

            if not self._is_running:
                break

            elapsed = time.monotonic() - t0
            if elapsed > interval:
                logger.warning(
                    "Cycle de collecte de %.1fs supérieur à l'intervalle de %.1fs", elapsed, interval
                )
            sleep_time = max(1.0, interval - elapsed)
            sleep_time *= 0.9 + random.random() * 0.2       # jitter anti-effet troupeau
            try:
                await asyncio.sleep(sleep_time)
            except asyncio.CancelledError:
                raise

    async def start(self) -> None:
        if self._is_running:
            return

        self._ensure_executors()
        self._http_transport = DynamicPinnedIPTransport(limits=HTTP_LIMITS, verify=True)
        self._http_client = httpx.AsyncClient(
            transport=self._http_transport,          # le transport porte la config TLS
            timeout=build_timeout(3.0),
            follow_redirects=False,
            headers={"User-Agent": _USER_AGENT},
        )

        try:
            await self._run_blocking(
                load_probe_policy_sync, timeout=8.0, executor=self._db_executor, on_timeout="none"
            )
        except Exception as exc:
            logger.warning("Politique de sondage par défaut conservée : %s", exc)

        self._is_running = True
        self._task = asyncio.create_task(self._worker_loop(), name="waas-telemetry")
        logger.info("Moteur de télémétrie unifié démarré (Background Telemetry Engine).")

    async def stop(self) -> None:
        self._is_running = False

        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        client, self._http_client = self._http_client, None
        if client is not None and not client.is_closed:
            with contextlib.suppress(Exception):
                await client.aclose()
        self._http_transport = None

        # Les threads daemon du résolveur DNS n'empêchent jamais l'arrêt du process.
        get_dns_pool().shutdown()

        for executor in (self._docker_executor, self._db_executor):
            with contextlib.suppress(Exception):
                executor.shutdown(wait=False, cancel_futures=True)

        logger.info("Moteur de télémétrie arrêté.")

    async def aclose(self) -> None:
        await self.stop()

    # -- Lecture (O(1), copie défensive) ----------------------------------
    def get_fleet_stats(self) -> dict[str, Any]:
        self.touch_activity()
        return _snapshot(self._stats_data)

    def get_fleet_health(self) -> dict[str, Any]:
        self.touch_activity()
        return _snapshot(self._health_data)

    def peek_fleet_health(self) -> dict[str, Any]:
        """Lecture sans marquer d'activité (page de statut publique : n'accélère pas la collecte)."""
        return _snapshot(self._health_data)

    def get_business_kpis(self) -> dict[str, Any]:
        self.touch_activity()
        return _snapshot(self._business_data)

    def get_diagnostics(self) -> dict[str, Any]:
        return {
            "running": self._is_running,
            "abandoned_blocking_threads": self._abandoned_threads,
            "dns": get_dns_pool().stats(),
            "collected_at": dict(self._collected),
            "last_errors": dict(self._last_errors),
            "policy": {
                "source": _POLICY.source,
                "allowed_ports": sorted(_POLICY.allowed_ports) if _POLICY.allowed_ports else None,
                "allowed_methods": sorted(_POLICY.allowed_methods),
                "probe_concurrency": _POLICY.probe_concurrency,
                "container_probe_concurrency": _POLICY.container_probe_concurrency,
                "block_self_targeting": _POLICY.block_self_targeting,
            },
        }

    def get_unified_telemetry(self) -> dict[str, Any]:
        self.touch_activity()
        return {
            "mode": "active" if self.is_active_mode() else "idle",
            "last_collected_at": self._last_collected_at,
            "stats": _snapshot(self._stats_data),
            "health": _snapshot(self._health_data),
            "business": _snapshot(self._business_data),
            "host": _snapshot(self._host_data),
            "diagnostics": self.get_diagnostics(),
        }

    # -- Lecture : supervision du cockpit ---------------------------------
    @property
    def last_collected_at(self) -> float:
        return self._last_collected_at

    def get_host(self) -> dict[str, Any]:
        return _snapshot(self._host_data)

    async def sample_host(self) -> dict[str, Any]:
        """Mesure fraîche (≤ 2 s) pour les jauges temps réel, hors event loop."""
        self.touch_activity()
        sample = await self._run_blocking(self.host.sample, timeout=8.0, on_timeout="none")
        if isinstance(sample, dict):
            self._host_data = sample
        return _snapshot(self._host_data)

    def get_timeline(self, period: str, hourly_rows: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        return self.host_history.timeline(period, hourly_rows)

    def get_inventory(self) -> list[dict[str, Any]]:
        return _snapshot(list((self._stats_data or {}).get("containers") or []))

    def get_traffic(self, tenant: Optional[str] = None) -> dict[str, Any]:
        series = self.traffic.series(tenant)
        return {
            "loaded": self.traffic.loaded,
            "series": series,
            "pageviews": sum(p["pageviews"] for p in series),
            "visitors": sum(p["visitors"] for p in series),
            "live": self.traffic.live(tenant),
        }

    def get_live_by_tenant(self) -> dict[str, int]:
        return self.traffic.live_by_tenant()

    def get_availability(self, prefix: Optional[str] = None) -> list[dict[str, Any]]:
        return [self.uptime.summary(key) for key in self.uptime.keys(prefix)]

    def get_site_availability(self, site_id: str) -> dict[str, Any]:
        return self.uptime.summary(f"site:{site_id}")

    def forget_site(self, site_id: str) -> None:
        """Site purgé : plus de disponibilité ni de trafic en mémoire (ni à persister)."""
        self.uptime.forget(f"site:{site_id}")
        self.traffic.forget(site_id)

    def get_sla(self) -> dict[str, Any]:
        """SLA global : services critiques (API, base, Docker) et vitrines."""
        keys = [k for k in self.uptime.keys() if k.startswith(("core:", "site:"))]
        series, uptime = self.uptime.global_series(keys)
        return {"series": series, "uptime": uptime, "services": len(keys), "persisted": self.uptime.persisted}

    def get_probe_log(self, limit: int = 40) -> list[dict[str, Any]]:
        return [dict(entry) for entry in list(self._probe_log)[: max(1, limit)]]


telemetry_engine = TelemetryEngine()
