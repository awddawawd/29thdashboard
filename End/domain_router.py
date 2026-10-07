"""Routage multi-domaines et sous-domaines WaaS (v2).

Gère la séparation automatique entre :
* apex (ex. 29th.site) & www : site vitrine de présentation (notre site produit) ;
* app.<domaine> : tableau de bord d'administration et portail client (Cockpit) ;
* api.<domaine> : plan de contrôle et API publique de synchronisation ;
* <slug>.<domaine> : vitrines hébergées des clients, routées vers leurs conteneurs respectifs.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx
from starlette.datastructures import Headers
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("waas.domain_router")

DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")

# Cache court en mémoire des ports hôtes des conteneurs (évite d'appeler Docker à chaque asset)
_PORT_CACHE: dict[str, tuple[float, Optional[str]]] = {}
_PORT_CACHE_TTL = 5.0

# Client HTTP persistant partagé pour le relai inverse vers les conteneurs locaux
_PROXY_CLIENT: Optional[httpx.AsyncClient] = None
_PROXY_CLIENT_LOCK = asyncio.Lock() if hasattr(asyncio, "Lock") else None


def clean_domain(val: Optional[str]) -> str:
    """Extrait le nom de domaine nu (sans schéma, port, chemin ni slash de fin)."""
    if not val:
        return ""
    cleaned = str(val).strip().lower()
    if "://" in cleaned:
        cleaned = urlparse(cleaned).hostname or cleaned
    cleaned = cleaned.split("/")[0].split(":")[0].strip(".")
    return cleaned


def normalize_base_domain(raw: str) -> str:
    """Normalise une URL ou un nom de domaine en domaine de base racine (ex. 29th.site).

    Retire les préfixes accidentels (app., api., www., https://) pour obtenir la base.
    """
    cleaned = clean_domain(raw)
    for prefix in ("app.", "api.", "www."):
        if cleaned.startswith(prefix) and len(cleaned) > len(prefix):
            cleaned = cleaned[len(prefix):]
            break
    if not cleaned or not DOMAIN_RE.match(cleaned):
        raise ValueError(
            f"Nom de domaine invalide : '{raw}'. Exemple attendu : '29th.site' ou 'mondomaine.com'."
        )
    return cleaned


async def get_proxy_client() -> httpx.AsyncClient:
    global _PROXY_CLIENT
    if _PROXY_CLIENT is None or _PROXY_CLIENT.is_closed:
        _PROXY_CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=60.0, pool=10.0),
            follow_redirects=False,
            limits=httpx.Limits(max_keepalive_connections=50, max_connections=200),
        )
    return _PROXY_CLIENT


def invalidate_port_cache(site_id: Optional[str] = None) -> None:
    if site_id:
        _PORT_CACHE.pop(site_id, None)
    else:
        _PORT_CACHE.clear()


def resolve_tenant_port(site_id: str, docker_client: Any) -> Optional[str]:
    """Résout le port publié pour un conteneur locataire (avec cache court en mémoire)."""
    now = time.time()
    cached = _PORT_CACHE.get(site_id)
    if cached and (now - cached[0] < _PORT_CACHE_TTL):
        return cached[1]

    if not docker_client:
        return None

    try:
        container = docker_client.containers.get(f"waas-{site_id}")
        if container.status != "running":
            _PORT_CACHE[site_id] = (now, None)
            return None

        ports = (getattr(container, "attrs", None) or {}).get("NetworkSettings", {}).get("Ports") or {}
        for key in ("8080/tcp", "80/tcp"):
            mapping = ports.get(key) or []
            if mapping and isinstance(mapping[0], dict):
                host_port = mapping[0].get("HostPort")
                if host_port:
                    port_str = str(host_port)
                    _PORT_CACHE[site_id] = (now, port_str)
                    return port_str
    except Exception:
        pass

    _PORT_CACHE[site_id] = (now, None)
    return None


async def proxy_http_request(scope: Scope, receive: Receive, send: Send, host_port: str) -> None:
    """Relaye en flux continu (streaming) la requête vers le serveur Nginx du conteneur locataire."""
    client = await get_proxy_client()
    path = scope.get("path", "")
    qs = scope.get("query_string", b"").decode("latin1")
    url = f"http://127.0.0.1:{host_port}{path}"
    if qs:
        url += f"?{qs}"

    # Construction des en-têtes amont
    headers: list[tuple[str, str]] = []
    scope_headers = scope.get("headers", [])
    raw_headers = dict(scope_headers)
    for k, v in scope_headers:
        name = k.decode("latin1").lower()
        if name not in ("host", "connection", "transfer-encoding"):
            headers.append((name, v.decode("latin1")))

    client_ip = scope.get("client", ("127.0.0.1", 0))[0] if scope.get("client") else "127.0.0.1"
    scheme = scope.get("scheme", "https")
    orig_host = raw_headers.get(b"x-forwarded-host") or raw_headers.get(b"host") or b""
    headers.append(("x-forwarded-for", client_ip))
    headers.append(("x-forwarded-proto", scheme))
    headers.append(("x-forwarded-host", orig_host.decode("latin1", "replace")))

    # Lecture intégrale du corps de requête (les requêtes vitrines sont petites : GET ou POST formulaire)
    body_parts: list[bytes] = []
    more_body = True
    while more_body:
        message = await receive()
        assert message["type"] == "http.request"
        body_parts.append(message.get("body", b""))
        more_body = message.get("more_body", False)
    body = b"".join(body_parts)

    try:
        req = client.build_request(
            method=scope.get("method", "GET"),
            url=url,
            headers=headers,
            content=body if body else None,
        )
        upstream = await client.send(req, stream=True)
    except (httpx.ConnectError, httpx.TimeoutException, OSError) as exc:
        logger.warning("Échec de proxy vers 127.0.0.1:%s (%s)", host_port, exc)
        resp = HTMLResponse(
            content=(
                "<!doctype html><html lang='fr'><head><meta charset='utf-8'><title>Site en cours de démarrage</title>"
                "<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;text-align:center;padding:80px 20px;color:#222;}"
                "h2{margin-bottom:12px;}p{color:#666;font-size:15px;}</style></head>"
                "<body><h2>Vitrine en cours de démarrage</h2>"
                "<p>Ce site s'initialise ou est temporairement indisponible. Veuillez rafraîchir d'ici quelques secondes.</p></body></html>"
            ),
            status_code=503,
            headers={"Retry-After": "5"},
        )
        await resp(scope, receive, send)
        return

    resp_headers: list[tuple[bytes, bytes]] = []
    for k, v in upstream.headers.raw:
        if k.lower() not in (b"connection", b"transfer-encoding"):
            resp_headers.append((k, v))

    await send({
        "type": "http.response.start",
        "status": upstream.status_code,
        "headers": resp_headers,
    })

    try:
        async for chunk in upstream.aiter_bytes():
            await send({
                "type": "http.response.body",
                "body": chunk,
                "more_body": True,
            })
        await send({
            "type": "http.response.body",
            "body": b"",
            "more_body": False,
        })
    finally:
        await upstream.aclose()


class DomainRouterMiddleware:
    """Middleware ASGI de routage basé sur le nom d'hôte (Host header).

    En mode production, sépare automatiquement :
    - app.<domaine> -> FastAPI Cockpit / Portail
    - api.<domaine> -> FastAPI API
    - www.<domaine> -> redirection 301 vers le domaine racine
    - <domaine>     -> site vitrine produit / agence (proxy vers conteneur sélectionné)
    - <slug>.<domaine> -> vitrine client (proxy vers conteneur waas-<slug>)
    - domaines personnalisés -> vitrine client associée
    """

    def __init__(self, app: ASGIApp, get_db: Callable[[], Any], get_docker: Callable[[], Any],
                 is_prod_fn: Callable[[], bool], docs_enabled: bool = False) -> None:
        self.app = app
        self.get_db = get_db
        self.get_docker = get_docker
        self.is_prod_fn = is_prod_fn
        self.docs_enabled = docs_enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        # Les points d'entrée d'API et de télémétrie sont toujours traités directement par FastAPI,
        # quel que soit le sous-domaine utilisé (compatibilité maximale des trackers et conteneurs).
        if path.startswith(("/waas/analytics/", "/analytics/hit/", "/internal/sync/", "/internal/source/")):
            await self.app(scope, receive, send)
            return

        db_mod = self.get_db()
        base_domain = clean_domain(db_mod.get_setting("client_base_domain", ""))
        is_production = self.is_prod_fn() and bool(base_domain and base_domain not in ("localhost", "127.0.0.1"))

        if not is_production:
            # Mode local : aucun routage de sous-domaine, FastAPI sert tout sur localhost.
            await self.app(scope, receive, send)
            return

        raw_headers = dict(scope.get("headers", []))
        host_bytes = raw_headers.get(b"x-forwarded-host") or raw_headers.get(b"host") or b""
        host = clean_domain(host_bytes.decode("latin1", "replace"))

        # Si l'hôte n'est pas résolu ou est localhost / IP directe, passage direct
        if not host or host in ("localhost", "127.0.0.1", "host.docker.internal", "testserver"):
            await self.app(scope, receive, send)
            return

        docker_client = self.get_docker()

        # -------------------------------------------------------------
        # 1. Sous-domaine API (ex. api.29th.site)
        # -------------------------------------------------------------
        if host == f"api.{base_domain}":
            if path in ("", "/"):
                resp = JSONResponse({
                    "service": "WaaS API Engine",
                    "mode": "production",
                    "status": "online",
                    "dashboard": f"https://app.{base_domain}",
                    "docs": "/docs" if self.docs_enabled else None,
                })
                await resp(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        # -------------------------------------------------------------
        # 2. Sous-domaine Dashboard (ex. app.29th.site)
        # -------------------------------------------------------------
        if host == f"app.{base_domain}":
            # Le dashboard sert le cockpit (/admin), portail (/portal), studio (/studio), login (/login) et API
            await self.app(scope, receive, send)
            return

        # -------------------------------------------------------------
        # 3. www.<domaine> -> Redirection canonique 301 vers le domaine racine
        # -------------------------------------------------------------
        if host == f"www.{base_domain}":
            qs = scope.get("query_string", b"").decode("latin1")
            target = f"https://{base_domain}{path}"
            if qs:
                target += f"?{qs}"
            redirect = RedirectResponse(url=target, status_code=301)
            await redirect(scope, receive, send)
            return

        # -------------------------------------------------------------
        # 4. Domaine racine (ex. 29th.site) : Notre site vitrine / produit
        # -------------------------------------------------------------
        if host == base_domain:
            landing_slug = (db_mod.get_setting("landing_site_slug", "") or "").strip().lower()
            if not landing_slug:
                # Recherche d'un site explicitement rattaché au domaine racine en base
                try:
                    records = db_mod.list_site_records()
                    for r in records:
                        if clean_domain(r.get("domain", "")) in (base_domain, f"www.{base_domain}"):
                            landing_slug = r.get("site_id") or r.get("id") or ""
                            break
                except Exception:
                    pass

            if landing_slug:
                port = resolve_tenant_port(landing_slug, docker_client)
                if port:
                    await proxy_http_request(scope, receive, send, port)
                    return
                # Conteneur non joignable
                resp = HTMLResponse(
                    content=(
                        "<!doctype html><html lang='fr'><head><meta charset='utf-8'><title>Site en maintenance</title>"
                        "<style>body{font-family:-apple-system,sans-serif;text-align:center;padding:80px 20px;color:#222;}</style></head>"
                        "<body><h2>Site vitrine en cours de démarrage</h2>"
                        f"<p>La vitrine <strong>{landing_slug}</strong> est en cours d'initialisation.</p>"
                        f"<p><a href='https://app.{base_domain}/login' style='color:#0066cc;'>Accéder au tableau de bord</a></p></body></html>"
                    ),
                    status_code=503,
                )
                await resp(scope, receive, send)
                return

            # Aucun site vitrine configuré pour la racine : redirection vers le dashboard
            redirect = RedirectResponse(url=f"https://app.{base_domain}/login", status_code=302)
            await redirect(scope, receive, send)
            return

        # -------------------------------------------------------------
        # 5. Sous-domaine locataire client : <slug>.<domaine>
        # -------------------------------------------------------------
        if host.endswith(f".{base_domain}"):
            slug = host[:-len(base_domain)-1].lower().strip(".")
            if slug and slug not in ("app", "api", "www"):
                port = resolve_tenant_port(slug, docker_client)
                if port:
                    await proxy_http_request(scope, receive, send, port)
                    return
                resp = HTMLResponse(
                    content=(
                        "<!doctype html><html lang='fr'><head><meta charset='utf-8'><title>Vitrine introuvable</title>"
                        "<style>body{font-family:-apple-system,sans-serif;text-align:center;padding:80px 20px;color:#333;}"
                        "h2{margin-bottom:10px;}p{color:#666;font-size:15px;}</style></head>"
                        f"<body><h2>Vitrine '{slug}' non disponible</h2>"
                        "<p>Ce site n'est pas déployé ou est actuellement arrêté.</p></body></html>"
                    ),
                    status_code=404,
                )
                await resp(scope, receive, send)
                return

        # -------------------------------------------------------------
        # 6. Nom de domaine personnalisé propre au client (ex. client-shop.fr)
        # -------------------------------------------------------------
        try:
            records = db_mod.list_site_records()
            for r in records:
                if clean_domain(r.get("domain", "")) == host:
                    site_id = r.get("site_id") or r.get("id") or ""
                    if site_id:
                        port = resolve_tenant_port(site_id, docker_client)
                        if port:
                            await proxy_http_request(scope, receive, send, port)
                            return
        except Exception:
            pass

        # Si aucune règle n'a intercepté, passage normal à l'application FastAPI
        await self.app(scope, receive, send)
