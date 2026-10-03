"""Tests de non-régression et de durcissement du moteur de télémétrie WaaS.

Exécution (aucun accès réseau externe, aucun accès base) :

    .venv\\Scripts\\python.exe tests\\test_telemetry_hardening.py

Chaque test vérifie un correctif de la revue de sécurité ; le script sort en
code 1 si une garantie est perdue.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import os
import pathlib
import socket
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "End"))
os.chdir(ROOT / "End")

import httpx  # noqa: E402
import httpcore  # noqa: E402

import db  # noqa: E402
import telemetry as T  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    status = "PASS" if condition else "FAIL"
    if not condition:
        FAILURES.append(f"{name} {detail}")
    print(f"  [{status}] {name}" + (f" -> {detail}" if detail and not condition else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
# Serveurs HTTP locaux d'appoint
# ---------------------------------------------------------------------------

async def _start_server(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def _simple_handler(status: int = 200):
    async def handler(reader, writer):
        with contextlib.suppress(Exception):
            await reader.read(4096)
            writer.write(
                f"HTTP/1.1 {status} OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok".encode()
            )
            await writer.drain()
        writer.close()
    return handler


# ---------------------------------------------------------------------------
# 1. Transport / TLS / signatures HTTPX
# ---------------------------------------------------------------------------

async def test_transport() -> None:
    section("1. Transport HTTPX + configuration TLS conservée")

    transport = T.DynamicPinnedIPTransport()          # limits=None : plantait avant
    check("DynamicPinnedIPTransport() sans limits ne lève plus", True)
    pool = transport._pool
    check("pool httpcore construit", isinstance(pool, httpcore.AsyncConnectionPool))
    check("ssl_context transmis au pool (TLS non perdu)", pool._ssl_context is not None)
    check(
        "backend réseau épinglé installé",
        isinstance(pool._network_backend, T.DynamicPinnedIPNetworkBackend),
    )
    check("limites par défaut appliquées", pool._max_connections == T.HTTP_LIMITS.max_connections,
          str(pool._max_connections))

    custom = T.DynamicPinnedIPTransport(
        limits=httpx.Limits(max_connections=7, max_keepalive_connections=3, keepalive_expiry=11.0),
        verify=True, http2=False, retries=2,
    )
    check("limites personnalisées respectées", custom._pool._max_connections == 7)
    check("retries transmis au pool", custom._pool._retries == 2, str(custom._pool._retries))
    await transport.aclose()
    await custom.aclose()

    # Timeout dédié : la file du pool ne doit pas produire de faux "DOWN".
    tmo = T.build_timeout(1.5)
    check("build_timeout borne la file du pool", tmo.pool is not None and tmo.connect == 1.5)


# ---------------------------------------------------------------------------
# 2. Anti-SSRF : matrice d'attaques
# ---------------------------------------------------------------------------

async def test_ssrf_matrix() -> None:
    section("2. Matrice anti-SSRF (aucune résolution réseau requise)")

    refused = [
        "http://127.0.0.1:8080", "http://[::1]:8080", "http://0.0.0.0/",
        "http://169.254.169.254/latest/meta-data/", "http://[::ffff:169.254.169.254]/",
        "http://[::ffff:127.0.0.1]/", "http://[64:ff9b::7f00:1]/", "http://[fec0::1]/",
        "http://10.0.0.5:8080", "http://192.168.1.1/", "http://[fe80::1]/",
        "http://localhost:8080", "http://user:pw@127.0.0.1/",
        "http://example.com:2379/", "http://example.com:6443/", "http://example.com:2375/",
        "http://example.com:99999/", "gopher://example.com/", "file:///etc/passwd",
        "http://2130706433/", "http://0x7f000001/",
    ]
    for target in refused:
        safe, message, ip, port = await T.is_safe_external_target(target, is_url=True)
        check(f"HTTP refusé : {target}", not safe and ip is None, f"message={message}")

    tcp_refused = [
        "127.0.0.1:80", "localhost:3306", "[::1]:80", "0.0.0.0:80",
        "169.254.169.254:80", "[::ffff:169.254.169.254]:80", "[::1]", "example.com:0",
        "example.com:-1", "example.com:65536", "example.com:2379", "example.com:6443",
    ]
    for target in tcp_refused:
        safe, message, ip, port = await T.is_safe_external_target(target, is_url=False)
        check(f"TCP refusé : {target}", not safe and ip is None, f"message={message}")

    # Classification des plages spéciales (nouveaux filets de sécurité)
    forbidden = ["127.0.0.1", "::1", "::ffff:10.0.0.1", "64:ff9b::7f00:1", "fec0::1",
                 "2002:7f00:1::", "224.0.0.1", "ff02::1", "192.88.99.1", "100.64.0.1",
                 "192.0.0.9", "2001:db8::1", "0.0.0.0"]
    for raw in forbidden:
        check(f"IP interdite : {raw}",
              T.is_forbidden_ip(ipaddress.ip_address(raw)))
    check("IP publique toujours autorisée",
          not T.is_forbidden_ip(ipaddress.ip_address("8.8.8.8")))

    # Le parsing TCP ne dépend plus d'un état global mutable
    host, port, error = T._parse_tcp_target("[2001:db8::1]:8443")
    check("parsing IPv6 entre crochets", (host, port, error) == ("2001:db8::1", 8443, None),
          f"{host} {port} {error}")
    host, port, error = T._parse_tcp_target("exemple.tld:443")
    check("parsing hôte:port", (host, port, error) == ("exemple.tld", 443, None))
    _, _, error = T._parse_tcp_target("exemple.tld")
    check("port manquant explicité", error is not None and "Port" in error)


async def test_port_policy() -> None:
    section("3. Politique de ports et de méthodes (base simulée)")

    original_get_setting = db.get_setting
    settings: dict[str, str] = {}

    def fake_get_setting(key: str, default=None):
        return settings.get(key, default if default is not None else "")

    db.get_setting = fake_get_setting  # type: ignore[assignment]
    try:
        policy = T.load_probe_policy_sync()
        check("mode denylist par défaut", policy.allowed_ports is None)
        check("etcd interdit", policy.port_allowed(2379) is not None)
        check("kubelet interdit", policy.port_allowed(10250) is not None)
        check("port web autorisé", policy.port_allowed(443) is None)
        check("méthodes restreintes", policy.allowed_methods == frozenset({"GET", "HEAD"}))

        settings["telemetry_probe_port_allowlist"] = "80, 443, 8443"
        policy = T.load_probe_policy_sync()
        check("mode allowlist activé", policy.allowed_ports == frozenset({80, 443, 8443}))
        check("port hors liste blanche refusé", policy.port_allowed(8080) is not None)
        check("port en liste blanche accepté", policy.port_allowed(8443) is None)

        settings.pop("telemetry_probe_port_allowlist")
        settings["telemetry_allowed_http_methods"] = "GET,HEAD,POST"
        settings["telemetry_probe_concurrency"] = "3"
        settings["telemetry_block_self_targeting"] = "0"
        policy = T.load_probe_policy_sync()
        check("méthodes étendues par réglage", "POST" in policy.allowed_methods)
        check("concurrence bornée par réglage", policy.probe_concurrency == 3)
        check("auto-ciblage désactivable", policy.block_self_targeting is False)

        # Anti auto-sondage : la plateforme ne doit pas se sonder elle-même
        settings.clear()
        settings["api_public_url"] = "https://waas.internal.example"
        policy = T.load_probe_policy_sync()
        check("empreinte de la plateforme calculée", "waas.internal.example" in policy.self_hosts,
              str(sorted(policy.self_hosts)))
        check("auto-ciblage détecté",
              policy.is_self_target("waas.internal.example", ["93.184.216.34"]))
        check("cible tierce non bloquée",
              not policy.is_self_target("example.com", ["93.184.216.34"]))

        # Cible externe refusée AVANT toute résolution DNS (donc testable hors ligne)
        safe, message, ip, _ = await T.is_safe_external_target("http://example.com:2379/")
        check("port sensible refusé avant DNS", not safe and "2379" in (message or ""), str(message))
    finally:
        db.get_setting = original_get_setting  # type: ignore[assignment]
        T._POLICY = T._default_policy()


# ---------------------------------------------------------------------------
# 4. Sondes : loopback, streaming, méthodes, épinglage
# ---------------------------------------------------------------------------

async def test_probes() -> None:
    section("4. Sondes HTTP : loopback, streaming, méthodes, épinglage")

    # --- 4a. Sonde loopback (l'ancien chemin passait par la validation SSRF) ---
    server, port = await _start_server(_simple_handler(200))
    result = await T.probe_local_http(port, timeout=2.0)
    check("sonde loopback UP (régression corrigée)", result["status"] == "UP", str(result))
    check("latence loopback mesurée", isinstance(result["latency_ms"], (int, float)))
    check("port local invalide géré", (await T.probe_local_http("abc"))["status"] == "DOWN")

    # --- 4b. Streaming : le corps n'est jamais matérialisé ---
    async def huge_body(reader, writer):
        with contextlib.suppress(Exception):
            await reader.read(4096)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 20000000\r\nConnection: close\r\n\r\n")
            await writer.drain()
            for _ in range(50):                     # 1 Ko toutes les 20 ms -> ~20 Mo
                writer.write(b"x" * 1024)
                await writer.drain()
                await asyncio.sleep(0.02)
        writer.close()

    huge, huge_port = await _start_server(huge_body)
    t0 = time.monotonic()
    result = await T.probe_local_http(huge_port, timeout=2.0)
    elapsed = time.monotonic() - t0
    check("corps volumineux non lu (statut obtenu immédiatement)",
          result["status"] == "UP" and elapsed < 1.0,
          f"status={result['status']} elapsed={elapsed:.2f}s")

    # --- 4c. Méthode non autorisée neutralisée ---
    captured: dict[str, str] = {}

    async def capture_method(reader, writer):
        with contextlib.suppress(Exception):
            data = await reader.read(4096)
            captured["request_line"] = data.split(b"\r\n", 1)[0].decode("latin-1")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        writer.close()

    method_server, method_port = await _start_server(capture_method)
    await T._http_probe(f"http://127.0.0.1:{method_port}/", None, 2.0, method="DELETE")
    check("méthode DELETE neutralisée en GET", captured.get("request_line", "").startswith("GET "),
          captured.get("request_line", ""))

    # --- 4d. En-têtes hostiles filtrés ---
    safe_headers = T.sanitize_headers({"Host": "evil", "X-Test": "ok", "Connection": "keep-alive"})
    check("en-tête Host supprimé", "Host" not in safe_headers)
    check("en-tête applicatif conservé", safe_headers.get("X-Test") == "ok")
    check("Connection forcé à close", safe_headers.get("Connection") == "close")

    # --- 4e. Épinglage d'IP : substitution DNS + fail-closed par hôte ---
    transport = T.DynamicPinnedIPTransport()
    async with httpx.AsyncClient(transport=transport, timeout=2.0) as client:
        token = T._pinned_ip_ctx.set(("hote-fictif.invalid", "127.0.0.1"))
        try:
            resp = await client.get(f"http://hote-fictif.invalid:{port}/")
            check("IP épinglée utilisée au lieu du DNS", resp.status_code == 200,
                  str(resp.status_code))
        finally:
            T._pinned_ip_ctx.reset(token)

        token = T._pinned_ip_ctx.set(("hote-fictif.invalid", "127.0.0.1"))
        try:
            refused = False
            try:
                await client.get(f"http://autre-hote.invalid:{port}/")
            except Exception as exc:               # httpcore.ConnectError
                refused = "épinglée" in str(exc) or "refus" in str(exc).lower()
            check("hôte non épinglé refusé (fail-closed)", refused)
        finally:
            T._pinned_ip_ctx.reset(token)
    await transport.aclose()

    # --- 4f. ContextVar isolé entre tâches concurrentes ---
    observed: dict[str, str] = {}

    async def child(name: str, value: str):
        T._pinned_ip_ctx.set((f"{name}.invalid", value))
        await asyncio.sleep(0.05)
        observed[name] = T._pinned_ip_ctx.get()[1]

    await asyncio.gather(child("a", "1.1.1.1"), child("b", "9.9.9.9"))
    check("aucune collision de pin entre coroutines", observed == {"a": "1.1.1.1", "b": "9.9.9.9"},
          str(observed))

    # --- 4f. Redirection non suivie (protection anti-SSRF par rebond) ---
    followed = {"count": 0}

    async def redirecting(reader, writer):
        with contextlib.suppress(Exception):
            await reader.read(4096)
            followed["count"] += 1
            writer.write(
                b"HTTP/1.1 301 Moved Permanently\r\nLocation: http://169.254.169.254/\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
        writer.close()

    redir_server, redir_port = await _start_server(redirecting)
    result = await T.probe_local_http(redir_port, timeout=2.0)
    check("redirection 3xx non suivie", result["status_code"] == 301,
          str(result.get("status_code")))
    check("aucune requête de rebond émise", followed["count"] == 1, str(followed))

    # --- 4g. Séparation erreur technique / message public (anti-oracle) ---
    monitor_result = await T.execute_single_monitor(
        {"id": "m1", "tenant_id": "t1", "monitor_type": "http",
         "target": "http://10.0.0.5:8080/", "expected_status": None,
         "timeout_seconds": None, "method": "DELETE"}
    )
    check("sonde refusée sans exception malgré des champs NULL",
          monitor_result["status"] == "DOWN")
    check("détail technique conservé pour l'admin",
          "10.0.0.5" in str(monitor_result.get("error")))
    check("message public sans fuite d'information interne",
          "10.0.0.5" not in str(monitor_result.get("error_public"))
          and monitor_result.get("error_public") == T._PUBLIC_SSRF_ERROR,
          str(monitor_result.get("error_public")))

    # --- 4h. Chaîne complète TLS sous épinglage d'IP (si réseau disponible) ---
    try:
        live = await T.probe_external_http("https://example.com/", timeout=8.0)
    except Exception as exc:                      # pragma: no cover
        live = {"status": "UNREACHABLE", "error": repr(exc)}
    if live.get("status_code") is None and live.get("status") == "DOWN":
        print(f"      (vérification réseau externe ignorée : {live.get('error')})")
    else:
        check("HTTPS épinglé : certificat validé (SAN/SNI d'origine préservés)",
              live["status"] == "UP" and live["status_code"] == 200, str(live))

    for srv in (server, huge, method_server, redir_server):
        srv.close()
        with contextlib.suppress(Exception):
            await srv.wait_closed()


# ---------------------------------------------------------------------------
# 5. Résolveur DNS : aucune famine, aucun thread bloquant l'arrêt
# ---------------------------------------------------------------------------

async def test_dns_pool() -> None:
    section("5. Résolveur DNS auto-réparant (threads daemon)")

    original_gai = socket.getaddrinfo

    def fake_gai(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        if str(host).startswith("hang"):
            time.sleep(30)                     # résolveur blackhole
        return original_gai(host, port, family, type, proto, flags)

    pool = T.get_dns_pool()
    socket.getaddrinfo = fake_gai  # type: ignore[assignment]
    try:
        t0 = time.monotonic()
        hung = await asyncio.gather(
            *(T.resolve_addrinfo(f"hang{i}.example", 80, timeout=0.3) for i in range(24)),
            return_exceptions=True,
        )
        hung_time = time.monotonic() - t0
        timeouts = [r for r in hung if isinstance(r, asyncio.TimeoutError)]
        check("résolutions bloquées abandonnées côté appelant", len(timeouts) == 24, str(len(timeouts)))
        check("abandon rapide (pas d'attente des threads)", hung_time < 5.0, f"{hung_time:.2f}s")

        # Le point clé : une résolution SAINE fonctionne malgré les threads morts.
        t0 = time.monotonic()
        ok = False
        try:
            infos = await T.resolve_addrinfo("localhost", 80, timeout=2.0)
            ok = bool(infos)
        except Exception as exc:
            ok = False
            print(f"      (résolution saine en échec : {exc!r})")
        check("résolution saine non stérilisée par les threads bloqués", ok,
              f"{time.monotonic() - t0:.2f}s")
        check("threads du pool remplacés automatiquement", pool.stats()["threads"] > 24,
              str(pool.stats()))

        # Disjoncteur : au-delà du plafond, refus immédiat au lieu d'une file d'attente
        for _ in range(pool._max_workers + 16):
            pool.submit("hang-extra.example", 80)
        check("disjoncteur actif quand le pool est saturé", pool.stats()["rejected"] > 0,
              str(pool.stats()))
        safe, message, _, _ = await T.is_safe_external_target("http://hang-final.example:80/")
        check("message public clair en cas de saturation", not safe and message is not None,
              str(message))
    finally:
        socket.getaddrinfo = original_gai  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 6. Métriques : CPU, business, isolation, collecte partielle
# ---------------------------------------------------------------------------

def test_cpu_percent() -> None:
    section("6. Calcul CPU tolérant")

    sample = {
        "cpu_stats": {"cpu_usage": {"total_usage": 200}, "system_cpu_usage": 1000,
                      "online_cpus": 2},
        "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 500},
    }
    check("CPU nominal", T.calculate_cpu_percent(sample) == 40.0,
          str(T.calculate_cpu_percent(sample)))
    check("échantillon sans precpu_stats -> 0.0", T.calculate_cpu_percent(
        {"cpu_stats": sample["cpu_stats"], "precpu_stats": {}}) == 0.0)
    check("échantillon corrompu -> 0.0 (aucune exception)",
          T.calculate_cpu_percent({"cpu_stats": None, "precpu_stats": None}) == 0.0)
    check("online_cpus=0 forcé à 1", T.calculate_cpu_percent(sample, online_cpus=0) == 20.0,
          str(T.calculate_cpu_percent(sample, online_cpus=0)))
    check("conversions sûres", T.as_float("199,90", 0.0) == 199.90
          and T.as_float("N/A", 7.0) == 7.0 and T.as_int(None, 3) == 3)


async def test_business_metrics() -> None:
    section("7. KPIs métier : robustesse et intégrité")

    engine = T.TelemetryEngine()

    original_all_clients = db.get_all_clients
    original_get_setting = db.get_setting

    db.get_setting = lambda key, default=None: {"monthly_price_per_site": "199"}.get(
        key, default if default is not None else ""
    )  # type: ignore[assignment]
    db.get_all_clients = lambda: [  # type: ignore[assignment]
        {"id": "c1", "sites": [{"site_id": "s1", "plan_price": "250,50"},
                               {"site_id": "s2", "plan_price": None}], "sites_count": 2},
        {"id": "c2", "sites": [], "sites_count": 3, "plan_price": "100"},
        {"id": "c3", "sites": [{"site_id": "s1"}], "sites_count": "2"},   # site en doublon
        {"id": "c4", "sites": [{"site_id": "s4", "plan_price": "N/A"}], "sites_count": 0},
        {"id": "c5", "sites": "données corrompues", "sites_count": "x"},
    ]
    try:
        data = await engine._collect_business_metrics()
        check("collecte sans exception malgré des données sales", isinstance(data, dict))
        check("clients comptés", data["total_clients"] == 5, str(data["total_clients"]))
        # s1 (250,50) + s2 (199 par défaut) + 3 sites c2 x 100 + s4 (199) = 948,50
        # c3 référence s1 en doublon : ignoré (aucun double comptage)
        check("MRR consolidé et dédupliqué", data["mrr"] == 948.5, str(data["mrr"]))
        check("ARR dérivé du MRR", data["arr"] == round(948.5 * 12, 2), str(data["arr"]))
        check("sites consolidés", data["total_sites"] == 6, str(data["total_sites"]))
        check("anomalies signalées (compteur incohérent + type invalide)",
              data.get("anomalies", 0) >= 2, str(data.get("anomalies")))
        check("instantané marqué frais", data.get("stale") is False)
    finally:
        db.get_all_clients = original_all_clients  # type: ignore[assignment]
        db.get_setting = original_get_setting  # type: ignore[assignment]

    # Base injoignable : l'API continue de servir le dernier instantané connu
    db.get_all_clients = lambda: (_ for _ in ()).throw(RuntimeError("base HS"))  # type: ignore[assignment]
    try:
        data = await engine._collect_business_metrics()
        check("panne de base -> instantané précédent marqué stale",
              data.get("stale") is True and "error" in data, str(data.get("error")))
    finally:
        db.get_all_clients = original_all_clients  # type: ignore[assignment]

    await engine.stop()


async def test_collection_isolation() -> None:
    section("8. Collecte best-effort (aucune métrique figée)")

    engine = T.TelemetryEngine()
    engine._stats_data = {"active_tenants": 42, "tenants": [{"site_id": "seed"}], "stale": False}
    engine._health_data = {"status": "HEALTHY", "fleet": {"alerts": []}, "stale": False}
    engine._business_data = {"mrr": 1234.0, "stale": False}

    async def boom():
        raise RuntimeError("collecteur en panne")

    async def health_ok():
        return {"status": "DEGRADED", "fleet": {"alerts": []}, "stale": False}

    async def business_ok():
        return {"mrr": 4321.0, "stale": False}

    engine._collect_docker_stats = boom          # type: ignore[assignment]
    engine._collect_health_status = health_ok    # type: ignore[assignment]
    engine._collect_business_metrics = business_ok  # type: ignore[assignment]

    original_get_setting = db.get_setting
    db.get_setting = lambda key, default=None: default if default is not None else ""  # type: ignore[assignment]
    try:
        await engine.collect_all()               # ne doit jamais lever
        check("collecte partielle sans exception", True)
        check("collecteur en panne marqué stale + trace",
              engine._stats_data.get("stale") is True and engine._stats_data.get("error"))
        check("données du collecteur en panne conservées",
              engine._stats_data.get("active_tenants") == 42)
        check("collecteurs sains mis à jour",
              engine._health_data["status"] == "DEGRADED" and engine._business_data["mrr"] == 4321.0)
    finally:
        db.get_setting = original_get_setting  # type: ignore[assignment]
        await engine.stop()

    # Isolation des instantanés exposés aux routes HTTP
    engine = T.TelemetryEngine()
    engine._stats_data = {"active_tenants": 1, "tenants": [{"site_id": "t1"}], "stale": False}
    snapshot = engine.get_fleet_stats()
    snapshot["tenants"].append({"site_id": "pirate"})
    snapshot["active_tenants"] = 999
    check("instantané copié (pas d'écriture dans l'état interne)",
          engine._stats_data["active_tenants"] == 1 and len(engine._stats_data["tenants"]) == 1)
    check("diagnostics exposés",
          "dns" in engine.get_diagnostics() and "policy" in engine.get_diagnostics())
    await engine.stop()


# ---------------------------------------------------------------------------
# 10. Cycle de vie du moteur (démarrage / arrêt / redémarrage à chaud)
# ---------------------------------------------------------------------------

async def test_engine_lifecycle() -> None:
    section("10. Cycle de vie du moteur (start / stop / hot restart)")

    engine = T.TelemetryEngine()
    calls = {"stats": 0, "health": 0, "business": 0}

    async def stats_ok():
        calls["stats"] += 1
        return {"active_tenants": 0, "tenants": [], "stale": False,
                "cached_at": "maintenant"}

    async def health_ok():
        calls["health"] += 1
        return {"status": "HEALTHY", "fleet": {"alerts": []}, "stale": False}

    async def business_ok():
        calls["business"] += 1
        return {"mrr": 1.0, "stale": False}

    engine._collect_docker_stats = stats_ok          # type: ignore[assignment]
    engine._collect_health_status = health_ok        # type: ignore[assignment]
    engine._collect_business_metrics = business_ok   # type: ignore[assignment]

    original_get_setting = db.get_setting
    db.get_setting = lambda key, default=None: default if default is not None else ""  # type: ignore[assignment]
    try:
        await engine.start()
        check("moteur démarré", engine.is_running() and engine.http_client is not None)
        check("transport épinglé porté par le client partagé",
              isinstance(engine.http_client._transport, T.DynamicPinnedIPTransport))
        await asyncio.sleep(0.5)
        check("collecte exécutée en tâche de fond", calls["stats"] >= 1, str(calls))

        await engine.stop()
        check("arrêt propre (client fermé, tâche annulée)",
              not engine.is_running() and engine.http_client is None)

        await engine.start()
        check("redémarrage à chaud possible (exécuteurs recréés)", engine.is_running())
        await engine.stop()
        check("second arrêt idempotent", not engine.is_running())
    finally:
        db.get_setting = original_get_setting  # type: ignore[assignment]
        with contextlib.suppress(Exception):
            await engine.stop()


# ---------------------------------------------------------------------------
# 11. Application FastAPI : import et câblage
# ---------------------------------------------------------------------------

def test_app_import() -> None:
    section("11. Application FastAPI : import et câblage")

    import main  # noqa: PLC0415

    check("application chargée", main.app is not None)
    check("client partagé de télémétrie accessible",
          hasattr(main.telemetry_engine, "http_client"))
    check("sonde loopback importée dans main", callable(main.probe_local_http))
    check("docker_client initialisé sans blocage long",
          main.docker_client is None or hasattr(main.docker_client, "containers"))
    check("cache de réglages exposé en copie",
          isinstance(db.get_settings_snapshot(), dict))

    source = (ROOT / "End" / "main.py").read_text(encoding="utf-8")
    check("plus de sonde loopback via le garde SSRF",
          'probe_http(f"http://127.0.0.1' not in source)
    check("I/O Supabase des uploads hors event loop",
          "await asyncio.to_thread(\n        db.upload_tenant_image" in source)


# ---------------------------------------------------------------------------

async def main() -> int:
    print("Tests de durcissement — moteur de télémétrie WaaS")
    await test_transport()
    await test_ssrf_matrix()
    await test_port_policy()
    await test_probes()
    await test_dns_pool()
    test_cpu_percent()
    await test_business_metrics()
    await test_collection_isolation()
    await test_engine_lifecycle()
    test_app_import()

    print(f"\n{'=' * 60}")
    print(f"{CHECKS - len(FAILURES)}/{CHECKS} vérifications réussies")
    if FAILURES:
        print("ÉCHECS :")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("Toutes les garanties de durcissement sont vérifiées.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
