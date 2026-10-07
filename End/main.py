import asyncio
import concurrent.futures
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from enum import Enum
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import re
import threading
import time
from typing import Annotated, Any, Literal, Optional
import docker
from docker.errors import APIError, DockerException, NotFound
from docker.types import Mount, Ulimit
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile, status
from pathlib import Path
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator
from urllib.parse import quote, urlparse

# pyrefly: ignore [missing-import]
import cockpit
import content_sync
import db
import git_source
import pages
import sync_auth
from domain_router import (
    DomainRouterMiddleware,
    clean_domain,
    invalidate_port_cache,
    normalize_base_domain,
)
from telemetry import (
    calculate_cpu_percent,
    describe_container,
    execute_single_monitor,
    is_safe_external_target,
    probe_local_http,
    telemetry_engine,
)

logger = logging.getLogger("waas.api")

# ---------------------------------------------------------
# SÉCURITÉ & AUTHENTIFICATION (JWT & RBAC)
# ---------------------------------------------------------

_DEFAULT_SECRET_KEY = "waas-secret-key-change-in-production"
_DEFAULT_PEPPER = "change-this-secret-salt-waas-2026"

#: Surchargeables par l'environnement ; les valeurs par défaut ne sont conservées
#: que pour ne pas invalider les sessions existantes (rotation recommandée).
SECRET_KEY = os.getenv("WAAS_SECRET_KEY") or _DEFAULT_SECRET_KEY
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24
ANALYTICS_PEPPER = os.getenv("WAAS_ANALYTICS_PEPPER") or _DEFAULT_PEPPER

if SECRET_KEY == _DEFAULT_SECRET_KEY or ANALYTICS_PEPPER == _DEFAULT_PEPPER:
    logger.warning(
        "Secret par défaut détecté : définir WAAS_SECRET_KEY (et WAAS_SYNC_SECRET) ainsi que "
        "WAAS_ANALYTICS_PEPPER dans l'environnement (rotation requise si le dépôt a été exposé)."
    )
if sync_auth.uses_default_secret():
    logger.warning(
        "WAAS_SYNC_SECRET absent : les jetons de synchronisation des conteneurs tenants sont "
        "dérivés de WAAS_SECRET_KEY. Définir un secret dédié avant mise en production."
    )

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)

#: Format d'un identifiant de site (= suffixe du conteneur ``waas-<id>``).
SITE_ID_RE = re.compile(r"^[a-z0-9-]{1,64}$")


def api_public_url_setting() -> str:
    """URL publique de l'API : réglage en base, sinon variable d'environnement.

    Les conteneurs locataires doivent pouvoir joindre cette URL (voir
    ``validate_container_api_url``) ; la variable ``API_PUBLIC_URL`` permet de
    configurer un déploiement sans écrire dans ``fleet_settings``.
    """
    try:
        value = db.get_setting("api_public_url", "") or ""
    except Exception:
        value = ""
    return (value or os.getenv("API_PUBLIC_URL", "")).strip()


def browser_api_url_raw() -> str:
    """Valeur brute du réglage ``browser_api_url`` (``""`` s'il n'est pas renseigné).

    Distingue « réglage absent » (repli) de « réglage renseigné » : les journaux et
    le cockpit doivent pouvoir dire *pourquoi* l'URL retenue est celle-là.
    """
    try:
        return (db.get_setting("browser_api_url", "") or "").strip()
    except Exception:
        return ""


def browser_api_url_setting() -> str:
    """URL de l'API **telle que le navigateur du visiteur ou de l'administrateur la voit**.

    ``api_public_url`` répond à « comment un conteneur joint l'API » et refuse
    ``localhost`` pour cette raison. Le navigateur, lui, vit sur l'hôte : en
    local l'API n'y est joignable que par ``http://localhost:8000``, alors que
    ``host.docker.internal`` n'y résout pas du tout. Deux consommateurs ont
    besoin de cette seconde vue :

    * le micro-tracker injecté dans les vitrines (``BROWSER_API_URL``) — sans
      quoi ``sendBeacon`` échoue en résolution DNS, silencieusement ;
    * ``frame-ancestors`` du serveur web de la vitrine (``PORTAL_ORIGIN``) —
      l'aperçu du Studio est encadré depuis l'origine de l'API.

    Vide : repli sur ``api_public_url`` (architecture recommandée, où les deux
    URL sont identiques : ``https://api.exemple.com``).
    """
    return browser_api_url_raw() or api_public_url_setting()


def is_production_mode(request: Request | None = None) -> bool:
    """Détermine si la plateforme est en production à partir de la configuration Supabase (passage de localhost à un nom de domaine)."""
    api_url = api_public_url_setting().lower()
    base_domain = clean_domain(db.get_setting("client_base_domain", ""))

    # Détection via la configuration Supabase (présence d'un nom de domaine hors localhost/127.0.0.1 ou protocole https)
    has_custom_domain = bool(base_domain and base_domain not in ("localhost", "127.0.0.1"))
    has_https_url = api_url.startswith("https://")
    is_db_production = has_custom_domain or has_https_url

    if is_db_production:
        return True

    # Détection dynamique sur la requête HTTP courante (nom d'hôte ou en-tête HTTPS/reverse-proxy)
    if request is not None:
        host = clean_domain(request.url.hostname or "")
        is_request_domain = bool(host and host not in ("localhost", "127.0.0.1", "testserver"))
        is_request_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        return is_request_domain or is_request_https

    return False


def set_auth_cookie(response: Response, token: str, request: Request | None = None) -> None:
    """Configure le cookie de session HttpOnly avec secure=True dès qu'un nom de domaine est actif (piloté par Supabase)."""
    secure_flag = is_production_mode(request)
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=secure_flag,
        path="/",
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


# ---------------------------------------------------------
# ANTI-FORCE BRUTE (limitation des tentatives de connexion)
# ---------------------------------------------------------

class LoginThrottle:
    """Limiteur en mémoire : N échecs par (IP, identifiant) => blocage temporaire.

    Les comptes sont réinitialisés à la première connexion réussie ; le blocage
    expire automatiquement, ce qui évite tout déni de service permanent.
    """

    def __init__(self, max_failures: int = 5, window_seconds: float = 300.0,
                 lockout_seconds: float = 900.0) -> None:
        self._max_failures = max_failures
        self._window = window_seconds
        self._lockout = lockout_seconds
        self._failures: dict[str, list[float]] = {}
        self._blocked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str) -> float:
        now = time.time()
        with self._lock:
            self._prune(key, now)
            until = self._blocked_until.get(key, 0.0)
            return max(0.0, until - now)

    def register_failure(self, key: str) -> None:
        now = time.time()
        with self._lock:
            self._prune(key, now)
            attempts = self._failures.setdefault(key, [])
            attempts.append(now)
            if len(attempts) >= self._max_failures:
                self._blocked_until[key] = now + self._lockout
                self._failures.pop(key, None)
                logger.warning("Connexions temporairement bloquées pour %s (trop d'échecs)", key)

    def register_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._blocked_until.pop(key, None)

    def _prune(self, key: str, now: float) -> None:
        recent = [stamp for stamp in self._failures.get(key, []) if now - stamp < self._window]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        if self._blocked_until.get(key, 0.0) <= now:
            self._blocked_until.pop(key, None)


login_throttle = LoginThrottle()


def client_ip(request: Request) -> str:
    """IP cliente : ``X-Forwarded-For`` n'est cru que derrière un proxy local.

    Sinon l'en-tête, contrôlé par le client, permettrait de contourner le
    limiteur en changeant d'identité à chaque tentative.
    """
    peer = request.client.host if request.client else "inconnue"
    try:
        is_local_proxy = ipaddress.ip_address(peer).is_private or ipaddress.ip_address(peer).is_loopback
    except ValueError:
        is_local_proxy = False
    if is_local_proxy:
        forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return peer


def create_access_token(data: dict[str, Any]) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def verify_csrf_origin(request: Request) -> None:
    """Protection CSRF moderne (OWASP / W3C) pour requêtes d'action (POST/PUT/PATCH/DELETE)
    authentifiées par cookie de session. Vérifie que l'en-tête Origin ou Referer correspond au serveur hôte.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return

    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    host = request.headers.get("host")

    source = origin or referer
    if not source:
        log_masked_forbidden(
            request.url.path,
            "Requête d'action via cookie rejetée : en-tête Origin/Referer absent (Protection anti-CSRF)",
            underlying_code=403,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requête rejetée par la protection anti-CSRF (Origin manquant).",
        )

    parsed = urlparse(source)
    source_host = parsed.netloc or parsed.path.split("/")[0]

    if source_host and host:
        if source_host.lower() == host.lower():
            return
        # Tolérance localhost <-> 127.0.0.1 en développement, **sur le même port uniquement** :
        # une vitrine servie sur localhost:<autre port> partage le cookie de session (les
        # cookies ignorent le port) et ne doit jamais pouvoir agir sur l'API.
        source_name, _, source_port = source_host.lower().rpartition(":") if ":" in source_host else (source_host.lower(), "", "")
        host_name, _, host_port = host.lower().rpartition(":") if ":" in host else (host.lower(), "", "")
        is_local = (
            source_name in ("localhost", "127.0.0.1")
            and host_name in ("localhost", "127.0.0.1")
            and source_port == host_port
        )
        if is_local:
            return

        # Tolérance sous-domaines légitimes du plan de contrôle en production :
        # app.<domaine> et api.<domaine> peuvent interagir en toute sécurité.
        base_domain = clean_domain(db.get_setting("client_base_domain", ""))
        if base_domain and base_domain not in ("localhost", "127.0.0.1"):
            clean_source = clean_domain(source_name)
            clean_target = clean_domain(host_name)
            control_plane_hosts = {f"app.{base_domain}", f"api.{base_domain}", base_domain}
            if clean_source in control_plane_hosts and clean_target in control_plane_hosts:
                return

    log_masked_forbidden(
        request.url.path,
        f"Attaque CSRF potentielle bloquée : Origine '{source_host}' non autorisée pour l'hôte '{host}'",
        underlying_code=403,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Requête rejetée par la protection anti-CSRF (Origine non autorisée).",
    )


def get_current_user(
    request: Request,
    token_header: Annotated[str | None, Depends(oauth2_scheme)] = None,
) -> dict[str, Any]:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Identifiants invalides ou session expirée.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    is_cookie_auth = False
    # 1. Header Authorization: Bearer <token>
    token = token_header
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()

    # 2. Cookie de session HttpOnly
    if not token:
        token = request.cookies.get("access_token")
        if token:
            is_cookie_auth = True

    if not token:
        raise credentials_exception

    # Validation anti-CSRF sur requêtes d'action via cookie
    if is_cookie_auth:
        verify_csrf_origin(request)

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except jwt.PyJWTError:
        raise credentials_exception

    user = db.get_user_by_username(username)
    if user is None:
        raise credentials_exception
    return user


def log_masked_forbidden(path: str, reason: str, underlying_code: int = 403) -> None:
    """Affiche une notification explicite dans la console lorsque la politique '404 Partout' (Zero-Leak)
    masque une erreur 403 Forbidden ou 401 Unauthorized en 404 Not Found.
    """
    label = "403 Forbidden" if underlying_code == 403 else "401 Unauthorized"
    msg = (
        f"\n   🛡️  [RBAC Guard / Zero-Leak] 404 Not Found émis (en réalité {label})\n"
        f"       ↳ Cible  : {path}\n"
        f"       ↳ Raison : {reason}\n"
        f"       ↳ Info   : Protection active (fonctionne comme prévu, aucune fuite d'existence).\n"
    )
    def _safe_print(text: str) -> None:
        try:
            print(text, flush=True)
        except UnicodeEncodeError:
            try:
                print(text.encode("utf-8", errors="replace").decode("cp1252", errors="replace"), flush=True)
            except Exception:
                print(text.encode("ascii", errors="replace").decode("ascii"), flush=True)

    try:
        loop = asyncio.get_running_loop()
        async def _delayed_print():
            # Léger délai (40ms) pour s'afficher juste après la ligne d'accès HTTP d'Uvicorn dans le terminal
            await asyncio.sleep(0.04)
            _safe_print(msg)
        loop.create_task(_delayed_print())
    except RuntimeError:
        _safe_print(msg)


def require_admin(
    request: Request,
    token_header: Annotated[str | None, Depends(oauth2_scheme)] = None,
) -> dict[str, Any]:
    # Approche "404 Partout" (OWASP / GitHub) : masque totalement l'existence des routes admin
    is_cookie_auth = False
    # 1. Extraction du token depuis header Authorization ou cookie HttpOnly
    token = token_header
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()

    if not token:
        token = request.cookies.get("access_token")
        if token:
            is_cookie_auth = True

    if not token:
        log_masked_forbidden(
            request.url.path,
            "Aucun jeton de session (cookie 'access_token' ou header 'Authorization') fourni",
            underlying_code=401,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ressource introuvable.",
        )

    # Validation anti-CSRF sur requêtes d'action via cookie
    if is_cookie_auth:
        verify_csrf_origin(request)

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        role = payload.get("role")
        if not username or role != "admin":
            log_masked_forbidden(
                request.url.path,
                f"Utilisateur '{username}' connecté avec rôle '{role}' (admin requis)",
                underlying_code=403,
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Ressource introuvable.",
            )
    except jwt.PyJWTError as e:
        log_masked_forbidden(
            request.url.path,
            f"Jeton de session invalide ou expiré ({e})",
            underlying_code=401,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ressource introuvable.",
        )

    user = db.get_user_by_username(username)
    if not user or user.get("role") != "admin":
        log_masked_forbidden(
            request.url.path,
            f"Utilisateur '{username}' non trouvé ou non administrateur en base",
            underlying_code=403,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ressource introuvable.",
        )

    return user


def get_current_admin_from_cookie_or_header(
    request: Request,
    token_header: Annotated[str | None, Depends(oauth2_scheme)] = None,
) -> dict[str, Any]:
    """Valide les permissions administrateur (alias de require_admin)."""
    return require_admin(request=request, token_header=token_header)


def verify_site_ownership(
    site_id: str,
    current_user: Annotated[dict[str, Any], Depends(get_current_user)],
    request: Request,
) -> dict[str, Any]:
    """Vérifie que le site existe et appartient bien à l'utilisateur connecté (ou admin).
    Applique la politique Zero-Leak (masquage 404) — **refus par défaut**.
    """
    site = db.get_site_by_id(site_id)
    if not site:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site introuvable.")

    # Les administrateurs ont accès à tous les sites (y compris orphelins)
    if current_user.get("role") == "admin":
        return site

    # Fail-closed pour les clients : `db.get_site_by_id` synthétise un site sans
    # `client_id` quand le site n'est rattaché à personne (table `sites` absente ou
    # enregistrement manquant). Accepter ce cas revenait à autoriser tout compte
    # sans `tenant_id` sur n'importe quel site de la flotte.
    site_client_id = str(site.get("client_id") or "")
    if not site_client_id:
        log_masked_forbidden(
            request.url.path,
            f"Site '{site_id}' sans propriétaire enregistré (fail-closed)",
            underlying_code=403,
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site introuvable.")

    client_id = str(current_user.get("id") or current_user.get("client_id") or "")

    # Vérification d'appartenance
    if client_id != site_client_id:
        log_masked_forbidden(
            request.url.path,
            f"Site '{site_id}' rattaché à '{site_client_id}', client connecté '{client_id}'",
            underlying_code=403,
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site introuvable.")

    return site


def require_client_tenant_id(current_user: dict[str, Any]) -> str:
    """Site par défaut des anciennes routes singleton ``/waas/my-site/*``.

    Même règle de propriété que ``verify_site_ownership`` (``db.get_sites_by_client_id``) :
    un ``tenant_id`` hérité n'est honoré que si le compte possède toujours ce site —
    un rattachement périmé ne donne plus accès au site d'un autre client.
    """
    tenant_id = str(current_user.get("tenant_id") or "")
    if current_user.get("role") == "admin" and tenant_id:
        return tenant_id
    user_id = str(current_user.get("id") or current_user.get("client_id") or "")
    owned = [
        str(site.get("site_id") or site.get("id"))
        for site in (db.get_sites_by_client_id(user_id) if user_id else [])
        if site.get("site_id") or site.get("id")
    ]
    if tenant_id and tenant_id in owned:
        return tenant_id
    if owned:
        return owned[0]
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Compte non rattaché à une boutique (utilisez le portail Admin).",
    )


def build_site_url(host_port: int | str | None) -> str | None:
    """Construit l'URL publique du site selon le domaine configuré en DB."""
    if not host_port:
        return None
    raw_domain = db.get_setting("client_base_domain", "localhost")
    cleaned = clean_domain(raw_domain) or "localhost"
    return f"http://{cleaned}:{host_port}"


#: Ports internes du serveur web d'un conteneur locataire : ``8080`` pour les
#: images durcies (nginx non privilégié), ``80`` pour les conteneurs historiques
#: encore en service. Les deux sont acceptés pendant la migration.
WEB_PORT_KEYS = ("8080/tcp", "80/tcp")


def get_container_web_port(container: Any) -> str | None:
    """Port hôte publié pour le serveur web du conteneur, ou ``None``."""
    ports = (getattr(container, "attrs", None) or {}).get("NetworkSettings", {}).get("Ports") or {}
    for key in WEB_PORT_KEYS:
        mapping = ports.get(key) or []
        if mapping and isinstance(mapping[0], dict):
            host_port = mapping[0].get("HostPort")
            if host_port:
                return str(host_port)
    return None



# ---------------------------------------------------------
# APPLICATION FASTAPI & CYCLE DE VIE (LIFESPAN)
# ---------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Démarrage du moteur de télémétrie en arrière-plan
    await telemetry_engine.start()
    # Audit non bloquant : signale toute fuite de secret dans les conteneurs locataires
    audit_task = asyncio.create_task(_audit_tenant_secrets())
    yield
    # Arrêt propre lors du shutdown
    audit_task.cancel()
    await telemetry_engine.stop()


async def _audit_tenant_secrets() -> None:
    """Vérifie qu'aucun conteneur locataire ne détient un secret du plan de contrôle.

    Les conteneurs créés avant le correctif d'isolation gardent la clé
    ``service_role`` dans leur environnement : ce contrôle le rend visible à
    chaque démarrage au lieu de laisser la faille silencieuse.
    """
    def _scan() -> list[str]:
        if not docker_client:
            return []
        leaks: list[str] = []
        for container in docker_client.containers.list(
            all=True, filters={"label": "service=waas-tenant"}
        ):
            env = (container.attrs.get("Config") or {}).get("Env") or []
            for entry in env:
                parts = entry.split("=", 1)
                name = parts[0].strip().upper()
                val = parts[1].strip() if len(parts) > 1 else ""
                if (
                    name in (
                        "SUPABASE_KEY",
                        "SUPABASE_SERVICE_KEY",
                        "SUPABASE_SECRET_KEY",
                        "SUPABASE_PUBLISHABLE_KEY",
                        "SUPABASE_URL",
                        "WAAS_SECRET_KEY",
                    )
                    or val.startswith("sb_secret_")
                    or val.startswith("sb_publishable_")
                ):
                    leaks.append(f"{container.name}:{name}")
        return leaks

    try:
        leaks = await asyncio.to_thread(_scan)
    except Exception as exc:
        logger.warning("Audit des secrets des conteneurs locataires impossible : %s", exc)
        return

    if leaks:
        containers_hit = sorted({leak.split(":", 1)[0] for leak in leaks})
        logger.critical(
            "FUITES DE SECRETS : %d variable(s) du plan de contrôle dans %d conteneur(s) locataire(s) "
            "(%s) — recréez ces conteneurs via /admin/api/sites/deploy puis renouvelez la clé "
            "Supabase (Secret Key / service_role).",
            len(leaks), len(containers_hit), ", ".join(containers_hit),
        )
    else:
        logger.info("Audit secrets : aucun secret du plan de contrôle dans les conteneurs locataires.")


def _flag_enabled(name: str, default: bool = True) -> bool:
    """Lit un drapeau d'environnement (1/true/yes/on/oui)."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "oui")


#: Documentation interactive (Swagger UI / ReDoc) : indispensable en développement,
#: à couper en production. Elle est **publique** et sa CSP doit autoriser un CDN
#: tiers ainsi que `unsafe-eval` — deux affaiblissements inutiles en ligne.
DOCS_ENABLED = _flag_enabled("WAAS_ENABLE_DOCS", True)

app = FastAPI(
    title="WaaS Fleet Manager",
    swagger_ui_parameters={"persistAuthorization": True},
    lifespan=lifespan,
    docs_url="/docs" if DOCS_ENABLED else None,
    redoc_url="/redoc" if DOCS_ENABLED else None,
    openapi_url="/openapi.json" if DOCS_ENABLED else None,
)

if DOCS_ENABLED:
    logger.warning(
        "Documentation interactive exposée publiquement (/docs, /redoc, /openapi.json). "
        "Définir WAAS_ENABLE_DOCS=0 en production."
    )

#: CORS avec cookies : uniquement le serveur de développement Flask (run.py, port 3000).
#: Les vitrines locales (localhost:<port>) ne doivent jamais lire l'API avec la session
#: de l'administrateur.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1):3000$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

#: Routage automatique sous-domaines / vitrines en production
app.add_middleware(
    DomainRouterMiddleware,
    get_db=lambda: db,
    get_docker=lambda: docker_client,
    is_prod_fn=is_production_mode,
    docs_enabled=DOCS_ENABLED,
)

#: Suivi d'audience des vitrines : seules routes appelables depuis n'importe quelle origine.
ANALYTICS_PREFIXES = ("/analytics/hit/", "/waas/analytics/hit/")


@app.middleware("http")
async def analytics_cors(request: Request, call_next):
    """CORS ouvert pour la seule collecte d'audience des vitrines (domaines clients).

    ``navigator.sendBeacon`` envoie toujours en mode « credentials: include » : le
    joker ``*`` y est refusé par les navigateurs, d'où l'origine renvoyée en écho.
    Sans risque ici : la route est publique, ne lit aucun cookie et ne renvoie rien
    de confidentiel ; elle n'accepte que POST.
    """
    if not request.url.path.startswith(ANALYTICS_PREFIXES):
        return await call_next(request)
    origin = request.headers.get("origin")
    cors = {
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "86400",
        "Vary": "Origin",
    }
    if origin:
        cors["Access-Control-Allow-Origin"] = origin
        cors["Access-Control-Allow-Credentials"] = "true"
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=cors)
    response = await call_next(request)
    response.headers.update(cors)
    return response

#: CSP du portail. Les pages embarquent des gestionnaires `onclick` et des
#: `<style>` en ligne : `'unsafe-inline'` est donc indispensable et la CSP ne
#: remplace pas les corrections XSS (échappement, contrôle d'origine), elle les
#: complète (interdiction d'objets, de `<base>` détournée, d'encadrement).
PORTAL_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "img-src 'self' data: blob: https: http:; "
    "connect-src 'self' https: http:; "
    "frame-src 'self' https: http:; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"
)

#: CSP adaptée à la documentation OpenAPI interactive (Swagger UI / ReDoc hébergés sur CDN).
DOCS_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "img-src 'self' data: blob: https: http:; "
    "connect-src 'self' https: http:; "
    "frame-src 'self' https: http:; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """En-têtes de sécurité sur toutes les réponses du portail (CSP, HSTS, nosniff…)."""
    response = await call_next(request)
    headers = response.headers
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if headers.get("content-type", "").startswith("text/html"):
        path = request.url.path.rstrip("/")
        if DOCS_ENABLED and path in ("/docs", "/redoc"):
            headers.setdefault("Content-Security-Policy", DOCS_CSP)
        else:
            headers.setdefault("Content-Security-Policy", PORTAL_CSP)
    return response

FRONT_DIR = (Path(__file__).resolve().parent.parent / "Front").resolve()
ADMIN_DIR = (FRONT_DIR / "admin").resolve()
PORTAL_DIR = (FRONT_DIR / "portal").resolve()
STUDIO_DIR = (FRONT_DIR / "studio").resolve()
#: Fragments HTML partagés (<!-- @include … -->) : jamais servis tels quels.
COMPONENTS_DIR = (FRONT_DIR / "components").resolve()
#: Zones servies uniquement par leurs routes authentifiées (jamais par le catch-all public).
PROTECTED_DIRS = (ADMIN_DIR, PORTAL_DIR, STUDIO_DIR, COMPONENTS_DIR)
ADMIN_DIR.mkdir(parents=True, exist_ok=True)

NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}

# 1. Montage sécurisé des assets publics (CSS, JS, assets) avec mise en cache native
if (FRONT_DIR / "css").exists():
    app.mount("/css", StaticFiles(directory=FRONT_DIR / "css"), name="css")
if (FRONT_DIR / "js").exists():
    app.mount("/js", StaticFiles(directory=FRONT_DIR / "js"), name="js")
if (FRONT_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=FRONT_DIR / "assets"), name="assets")

try:
    # timeout court : docker.from_env() fait un GET /version bloquant au démarrage.
    docker_client = docker.from_env(timeout=10)
except Exception as e:
    docker_client = None
    print(f"Erreur daemon Docker : {e}")


# ---------------------------------------------------------
# GESTION DES ERREURS HTTP STANDARDISÉES
# ---------------------------------------------------------

#: Préfixes servant du JSON : jamais de page d'erreur HTML (évite les SyntaxError côté fetch).
API_PREFIXES = ("/api/", "/admin/api/", "/internal/", "/analytics/", "/waas/")


def render_error_page(status_code: int, title: str, message: str, href: str, label: str) -> HTMLResponse:
    """Page d'erreur au gabarit du cockpit ; toutes les valeurs sont échappées (anti-XSS réfléchie)."""
    template = COMPONENTS_DIR / "error.html"
    params = {
        "status": str(status_code), "title": title, "message": message, "href": href, "label": label,
    }
    if template.is_file():
        content = resolve_includes(template.read_text(encoding="utf-8"), params)
    else:
        content = (
            f"<!doctype html><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>"
            f"<a href='{html.escape(href, quote=True)}'>{html.escape(label)}</a>"
        )
    return HTMLResponse(status_code=status_code, content=content, headers=NO_CACHE_HEADERS)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Garantit un format standard : page HTML sécurisée pour les pages web, JSON standardisé pour l'API et les assets."""
    detail_msg = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    accept_header = request.headers.get("accept", "")

    # Détecte si l'URL cible un asset statique (JS, CSS, image, fonte, map) pour ne jamais lui envoyer de HTML (évite Uncaught SyntaxError)
    path_suffix = Path(request.url.path).suffix.lower()
    is_static_asset = path_suffix in (
        ".js", ".css", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".woff", ".woff2", ".ttf", ".json", ".map"
    )
    is_api = request.url.path.startswith(API_PREFIXES)

    # Si c'est une requête de page HTML de navigation web dans le navigateur (hors API et hors assets statiques)
    if "text/html" in accept_header and not is_api and not is_static_asset:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(
                url=f"/login?redirect={quote(target, safe='/')}", status_code=status.HTTP_302_FOUND
            )

        if exc.status_code == status.HTTP_404_NOT_FOUND:
            # Approche "404 Partout" : gabarit 404 uniforme, zéro fuite d'existence
            return render_error_page(
                404, "Page introuvable", "La page demandée n'existe pas ou a été déplacée.",
                "/", "Retourner à l'accueil",
            )

        return render_error_page(
            exc.status_code, f"Erreur {exc.status_code}", detail_msg, "/login", "Retourner à la connexion",
        )

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "message": detail_msg,
            "detail": detail_msg,
        },
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(db.SchemaNotReady)
async def schema_not_ready_handler(request: Request, exc: db.SchemaNotReady):
    """Écriture sur une table de la migration non appliquée : message actionnable, jamais un 500."""
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"success": False, "message": str(exc), "detail": str(exc), "schema_missing": exc.relation},
    )


@app.exception_handler(db.DuplicateRecord)
async def duplicate_record_handler(request: Request, exc: db.DuplicateRecord):
    detail = "Cet identifiant est déjà utilisé."
    return JSONResponse(status_code=status.HTTP_409_CONFLICT,
                        content={"success": False, "message": detail, "detail": detail})


# ---------------------------------------------------------
# 0. AUTHENTIFICATION (/api/login, /cockpit/login & /token)
# ---------------------------------------------------------

class LoginRequest(BaseModel):
    username: str | None = None
    email: str | None = None
    password: str
    redirect: str | None = None


class LoginResponse(BaseModel):
    success: bool
    message: str
    access_token: str | None = None
    token_type: str = "bearer"
    role: str | None = None
    client_id: str | None = None
    tenant_id: str | None = None
    redirect_url: str = "/portal"


@app.post("/api/login", response_model=LoginResponse, tags=["Auth"])
@app.post("/cockpit/login", response_model=LoginResponse, tags=["Auth"])
def login_api(payload: LoginRequest, response: Response, request: Request):
    identifier = (payload.username or payload.email or "").strip()
    if not identifier or not payload.password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Identifiant ou e-mail et mot de passe obligatoires.",
        )

    throttle_key = f"{client_ip(request)}|{identifier.lower()}"
    wait = login_throttle.retry_after(throttle_key)
    if wait > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Trop de tentatives de connexion. Réessayez plus tard.",
            headers={"Retry-After": str(int(wait) + 1)},
        )

    user = db.authenticate_user(identifier, payload.password)
    if not user:
        login_throttle.register_failure(throttle_key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Identifiant ou mot de passe incorrect.",
        )
    login_throttle.register_success(throttle_key)

    access_token = create_access_token(
        data={
            "sub": user["username"],
            "role": user["role"],
            "client_id": user.get("id"),
            "tenant_id": user.get("tenant_id"),
        }
    )

    user_role = user.get("role", "client")
    raw_redirect = (payload.redirect or "").strip()

    # Protection open-redirect : autoriser uniquement les chemins relatifs internes commençant par / (pas de //)
    clean_redirect = raw_redirect if (raw_redirect.startswith("/") and not raw_redirect.startswith("//")) else ""

    if user_role == "admin":
        # L'agence accède au cockpit, à l'espace client (vue agence) et au Studio.
        path_only = clean_redirect.split("?", 1)[0]
        if path_only.startswith(("/admin/", "/portal", "/studio")) and path_only not in ("/admin/",):
            redirect_url = clean_redirect
        else:
            redirect_url = "/admin/dashboard"
    else:
        # Clients : seules les pages de l'espace client (et le Studio) sont des destinations valides.
        path_only = clean_redirect.split("?", 1)[0]
        if path_only.startswith(("/portal", "/studio")):
            redirect_url = clean_redirect
        else:
            redirect_url = "/portal"

    # Dépose le cookie HttpOnly sécurisé pour la navigation des pages HTML
    set_auth_cookie(response, access_token, request)

    return {
        "success": True,
        "message": "Connexion réussie !",
        "access_token": access_token,
        "token_type": "bearer",
        "role": user["role"],
        "client_id": user.get("id"),
        "tenant_id": user.get("tenant_id"),
        "redirect_url": redirect_url,
    }


@app.post("/token", tags=["Auth"])
def login(form_data: Annotated[OAuth2PasswordRequestForm, Depends()], response: Response, request: Request):
    throttle_key = f"{client_ip(request)}|{form_data.username.strip().lower()}"
    wait = login_throttle.retry_after(throttle_key)
    if wait > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Trop de tentatives de connexion. Réessayez plus tard.",
            headers={"Retry-After": str(int(wait) + 1)},
        )

    user = db.authenticate_user(form_data.username, form_data.password)
    if not user:
        login_throttle.register_failure(throttle_key)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nom d'utilisateur ou mot de passe incorrect.",
        )
    login_throttle.register_success(throttle_key)

    access_token = create_access_token(
        data={
            "sub": user["username"],
            "role": user["role"],
            "client_id": user.get("id"),
            "tenant_id": user.get("tenant_id"),
        }
    )

    user_role = user.get("role", "client")
    redirect_url = "/admin/dashboard" if user_role == "admin" else "/portal"

    set_auth_cookie(response, access_token, request)

    return {
        "success": True,
        "message": "Connexion réussie !",
        "access_token": access_token,
        "token_type": "bearer",
        "role": user["role"],
        "client_id": user.get("id"),
        "tenant_id": user.get("tenant_id"),
        "redirect_url": redirect_url,
    }


@app.post("/api/logout", tags=["Auth"])
def logout_api(response: Response):
    """Supprime le cookie de session pour fermer la session API et web."""
    response.delete_cookie(key="access_token", path="/")
    return {"success": True, "message": "Déconnexion réussie."}


@app.get("/logout", include_in_schema=False)
def logout_page(response: Response):
    """Déconnecte l'utilisateur et redirige vers la page de login."""
    res = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    res.delete_cookie(key="access_token", path="/")
    return res


# =========================================================
# 1. TRACKING PUBLIC (Privacy-Friendly / Sans Cookies)
# =========================================================

class HitRequest(BaseModel):
    path: str = "/"


@app.post("/analytics/hit/{site_id}", tags=["Analytics"])
@app.post("/waas/analytics/hit/{site_id}", tags=["Analytics"], include_in_schema=False)
def log_analytics_hit(site_id: str, payload: HitRequest, request: Request):
    """Enregistre une visite avec empreinte journalière hachée sans stocker de cookie."""
    # Identifiant hors format : ignoré en silence (route publique, pas d'oracle).
    if not SITE_ID_RE.fullmatch(site_id):
        return {"status": "ok"}
    # `client_ip` ne croit X-Forwarded-For que derrière un proxy local : un appelant
    # direct ne peut pas gonfler les visiteurs uniques en forgeant l'en-tête.
    visitor_ip = client_ip(request)
    user_agent = request.headers.get("user-agent", "")[:512]

    today_salt = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw_signature = f"{visitor_ip}:{user_agent}:{site_id}:{today_salt}:{ANALYTICS_PEPPER}"
    ephemeral_visitor_id = hashlib.sha256(raw_signature.encode("utf-8")).hexdigest()[:24]

    try:
        db.record_hit(
            tenant_id=site_id,
            visitor_id=ephemeral_visitor_id,
            path=payload.path or "/",
        )
    except Exception:
        pass

    return {"status": "ok"}


# =========================================================
# 2. ESPACE CLIENT & MULTI-SITES (1 Client = N Sites)
# =========================================================

MAX_CONTENT_KEY_LENGTH = 2048
MAX_CONTENT_PATH_LENGTH = 512
MAX_BATCH_CHANGES = 500


class UpdateContentRequest(BaseModel):
    xpath: str = Field(
        ...,
        min_length=1,
        max_length=MAX_CONTENT_KEY_LENGTH,
        description="XPath ciblant l'élément (texte ou balise img)",
        example="//section[@id='hero']/h1",
    )
    new_value: str = Field(
        ...,
        max_length=content_sync.MAX_VALUE_LENGTH,
        description="Nouveau texte ou nouvelle URL pour une image",
        example="Bienvenue chez Dupont",
    )
    file_path: str | None = Field(
        default=None,
        max_length=MAX_CONTENT_PATH_LENGTH,
        description="Page de l'élément (recommandé : sans elle, toutes les pages partageant ce XPath changent)",
        example="index.html",
    )


class ContentChangeItem(BaseModel):
    xpath: str = Field(..., min_length=1, max_length=MAX_CONTENT_KEY_LENGTH, description="XPath ciblant l'élément")
    new_value: str = Field(..., max_length=content_sync.MAX_VALUE_LENGTH,
                           description="Nouvelle valeur texte ou URL d'image")
    file_path: str | None = Field(default=None, max_length=MAX_CONTENT_PATH_LENGTH,
                                  description="Page de l'élément (recommandé)")
    expected_value: str | None = Field(
        default=None,
        max_length=content_sync.MAX_VALUE_LENGTH,
        description="Valeur lue avant modification : si elle a changé entre-temps, la modification "
                    "est refusée (statut « conflict ») au lieu d'écraser le travail d'un autre",
    )


class BatchUpdateContentRequest(BaseModel):
    changes: list[ContentChangeItem] = Field(..., max_length=MAX_BATCH_CHANGES)


#: Colonnes d'une fiche ``sites`` exposables au client propriétaire. Les notes
#: internes, le dépôt source et le registrar restent réservés à l'agence.
CLIENT_RECORD_FIELDS = (
    "site_id", "id", "name", "domain", "plan_id", "plan_name", "price_monthly",
    "contract_status", "contract_since", "domain_expires_at", "ssl_expires_at", "created_at",
)


def client_record_view(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in CLIENT_RECORD_FIELDS if key in record}


@app.get("/api/v1/account/me", tags=["Client Portal"])
def get_my_account(current_user: Annotated[dict[str, Any], Depends(get_current_user)]) -> dict[str, Any]:
    """Profil du compte client connecté et synthèse de ses sites."""
    sites = [client_record_view(site) for site in db.get_sites_by_client_id(current_user["id"])]
    return {
        "id": current_user["id"],
        "username": current_user["username"],
        "role": current_user["role"],
        "sites_count": len(sites),
        "sites": sites,
    }


@app.get("/api/v1/client/sites", tags=["Client Portal"])
@app.get("/api/v1/sites", tags=["Client Portal"])
def get_client_sites(current_user: Annotated[dict[str, Any], Depends(get_current_user)]) -> dict[str, Any]:
    """Liste des vitrines appartenant au client connecté avec statut Docker temps réel."""
    sites = db.get_sites_by_client_id(current_user["id"])
    enriched_sites = []

    for s in sites:
        site_id = s.get("site_id") or s.get("id")
        container_name = f"waas-{site_id}"
        is_running = False
        port = None

        if docker_client:
            try:
                container = docker_client.containers.get(container_name)
                is_running = (container.status == "running")
                port = get_container_web_port(container)
            except (NotFound, APIError):
                pass

        enriched_sites.append({
            **client_record_view(s),
            "site_id": site_id,
            "is_running": is_running,
            "port": port,
            "url": build_site_url(port) if is_running else None,
        })

    return {
        "count": len(enriched_sites),
        "sites": enriched_sites,
    }


@app.get("/api/v1/sites/{site_id}", tags=["Client Portal"])
def get_site_details(
    site_id: str,
    site: Annotated[dict[str, Any], Depends(verify_site_ownership)],
) -> dict[str, Any]:
    """État et URL publique du site demandé (vérification de propriété automatique)."""
    if not docker_client:
        raise HTTPException(status_code=503, detail="Service Docker indisponible.")

    container_name = f"waas-{site_id}"
    try:
        container = docker_client.containers.get(container_name)
    except NotFound:
        return {
            "site_id": site_id,
            "status": "not_provisioned",
            "is_online": False,
            "url": None,
        }

    is_running = container.status == "running"
    host_port = get_container_web_port(container)

    return {
        "site_id": site_id,
        "is_online": is_running,
        "url": build_site_url(host_port) if is_running else None,
        "created_at": container.attrs.get("Created"),
    }


# Rétrocompatibilité : route singleton /waas/my-site
@app.get("/waas/my-site", tags=["Client Portal"], include_in_schema=False)
def get_my_site_info_legacy(current_user: Annotated[dict[str, Any], Depends(get_current_user)]) -> dict[str, Any]:
    site_id = require_client_tenant_id(current_user)
    return get_site_details(site_id, site={"site_id": site_id})


#: Publication à chaud : le conteneur relit son contenu et réécrit ses pages depuis
#: le gabarit d'origine (moteur v2), sans redémarrage ni nouveau clonage — donc
#: sans interruption de service. Code 86 : image antérieure au moteur v2.
HOT_PUBLISH_TIMEOUT_SECONDS = 50
HOT_PUBLISH_UNSUPPORTED = 86
HOT_PUBLISH_MARKER = b"WAAS_REHYDRATE_OK"
_HOT_PUBLISH_SCRIPT = (
    f"test -f /opt/scripts/waas_content.py || exit {HOT_PUBLISH_UNSUPPORTED}; "
    "if timeout -k 1 1 true >/dev/null 2>&1; then "
    f"exec timeout -k 2 {HOT_PUBLISH_TIMEOUT_SECONDS} python3 /opt/scripts/sync_engine.py --rehydrate; "
    f"else exec timeout {HOT_PUBLISH_TIMEOUT_SECONDS} python3 /opt/scripts/sync_engine.py --rehydrate; fi"
)


def publish_site_content(site_id: str) -> dict[str, Any]:
    """Met en ligne le contenu enregistré : réhydratation à chaud, sinon redémarrage.

    Retour : ``{"published": bool, "method": "hot" | "restart" | None, "detail": str}``.
    Le redémarrage (reclonage du dépôt, coupure de quelques secondes) ne sert que de
    repli quand la réhydratation est **impossible** : image antérieure au moteur v2,
    exec refusé par un proxy de socket Docker, conteneur arrêté. Une réhydratation
    **en échec** (base momentanément injoignable, délai dépassé) ne redémarre rien : la
    vitrine reste servie dans sa version précédente — un redémarrage dans ces
    conditions la mettrait hors ligne, voire en boucle de redémarrage.
    """
    if not docker_client:
        return {"published": False, "method": None, "detail": "Service Docker indisponible."}
    try:
        container = docker_client.containers.get(f"waas-{site_id}")
    except NotFound:
        return {"published": False, "method": None, "detail": "Aucun conteneur pour ce site."}
    except APIError as exc:
        return {"published": False, "method": None, "detail": f"Docker : {exc}"}

    if container.status == "running":
        try:
            result = container.exec_run(
                ["sh", "-c", _HOT_PUBLISH_SCRIPT],
                user="10001:10001", workdir=TENANT_CONTENT_PATH, stdout=True, stderr=True, tty=False,
            )
        except APIError as exc:
            logger.warning("Publication à chaud de '%s' impossible (%s) — redémarrage.", site_id, exc)
        else:
            output = result.output or b""
            if result.exit_code == 0 and HOT_PUBLISH_MARKER in output:
                return {"published": True, "method": "hot", "detail": ""}
            if result.exit_code != HOT_PUBLISH_UNSUPPORTED:
                logger.warning("Publication à chaud de '%s' en échec (code %s) : %s",
                               site_id, result.exit_code, output[-600:].decode("utf-8", "replace"))
                return {"published": False, "method": None, "detail": (
                    "la mise en ligne a échoué, la vitrine reste servie dans sa version précédente. "
                    "Réessayez dans un instant.")}
    try:
        container.restart()
    except APIError as exc:
        return {"published": False, "method": None, "detail": f"Redémarrage impossible : {exc}"}
    return {"published": True, "method": "restart", "detail": ""}


def _publish_fields(outcome: dict[str, Any]) -> dict[str, Any]:
    return {"published": outcome["published"], "publish_method": outcome["method"],
            "publish_detail": outcome["detail"]}


@app.post("/api/v1/sites/{site_id}/publish", tags=["Client Portal"])
def publish_site_endpoint(
    site_id: str,
    site: Annotated[dict[str, Any], Depends(verify_site_ownership)],
) -> dict[str, Any]:
    """Publie les modifications enregistrées (à chaud, sinon par redémarrage du conteneur)."""
    if not docker_client:
        raise HTTPException(status_code=503, detail="Service Docker indisponible.")
    outcome = publish_site_content(site_id)
    if not outcome["published"]:
        status_code = 404 if outcome["detail"].startswith("Aucun conteneur") else 500
        raise HTTPException(status_code=status_code, detail=f"Publication impossible : {outcome['detail']}")
    return {
        "status": "success",
        "site_id": site_id,
        "message": f"Le site '{site_id}' a été republié avec les dernières modifications.",
        "method": outcome["method"],
        "action_timestamp": int(time.time()),
    }


@app.post("/waas/my-site/publish", tags=["Client Portal"], include_in_schema=False)
def publish_my_site_legacy(current_user: Annotated[dict[str, Any], Depends(get_current_user)]) -> dict[str, Any]:
    site_id = require_client_tenant_id(current_user)
    return publish_site_endpoint(site_id, site={"site_id": site_id})


@app.get("/api/v1/sites/{site_id}/content", tags=["Client Portal"])
def get_site_content_endpoint(
    site_id: str,
    site: Annotated[dict[str, Any], Depends(verify_site_ownership)],
) -> dict[str, Any]:
    """Liste tous les éléments modifiables du site (textes, images, attributs).

    ``anchors`` / ``audit`` : la migration ``002_editor.sql`` est appliquée (texte
    d'origine de chaque élément, auteur et date de la dernière modification). Les
    éléments dont la clé commence par ``orphan:`` sont détachés : ils ne
    correspondent plus à la page (gabarit modifié) et restent consultables.
    """
    try:
        items = db.get_tenant_content(site_id, strict=True)
    except db.ContentStoreError as exc:
        logger.error("%s", exc)
        raise HTTPException(status_code=502, detail="Contenu momentanément indisponible : réessayez.") from None
    return {
        "site_id": site_id,
        "count": len(items),
        "content": items,
        "anchors": db.content_anchors_ready(),
        "audit": db.content_audit_ready(),
    }


def _store_single_change(site_id: str, payload: UpdateContentRequest, author: Optional[str]) -> dict[str, Any]:
    """Enregistre une modification unitaire ; 404 / 422 explicites, jamais d'échec silencieux."""
    change = {"xpath": payload.xpath, "file_path": payload.file_path, "new_value": payload.new_value}
    try:
        result = db.update_content_values(site_id, [change], author=author)[0]
    except db.ContentStoreError as exc:
        logger.error("%s", exc)
        raise HTTPException(status_code=502, detail="Enregistrement momentanément impossible : réessayez.") from None
    if result["status"] == "invalid":
        raise HTTPException(status_code=422, detail=result.get("detail") or "Valeur refusée.")
    if result["status"] not in ("updated", "unchanged") or not result.get("row"):
        raise HTTPException(
            status_code=404,
            detail=f"Aucun élément trouvé pour le XPath '{payload.xpath}' sur le site '{site_id}'.",
        )
    return result["row"]


@app.get("/waas/my-site/content", tags=["Client Portal"], include_in_schema=False)
def get_my_site_content_legacy(current_user: Annotated[dict[str, Any], Depends(get_current_user)]) -> dict[str, Any]:
    site_id = require_client_tenant_id(current_user)
    return get_site_content_endpoint(site_id, site={"site_id": site_id})


@app.patch("/api/v1/sites/{site_id}/content", tags=["Client Portal"])
def update_site_content_endpoint(
    site_id: str,
    payload: UpdateContentRequest,
    site: Annotated[dict[str, Any], Depends(verify_site_ownership)],
    current_user: Annotated[dict[str, Any], Depends(get_current_user)],
    auto_publish: bool = Query(default=False, description="Republier immédiatement en ligne"),
) -> dict[str, Any]:
    """Met à jour un texte ou une URL d'image par son XPath (et sa page, recommandé)."""
    updated_record = _store_single_change(site_id, payload, current_user.get("username"))
    response_data: dict[str, Any] = {
        "status": "success",
        "site_id": site_id,
        "updated": updated_record,
        "published": False,
    }
    if auto_publish:
        response_data.update(_publish_fields(publish_site_content(site_id)))
    return response_data


@app.patch("/waas/my-site/content", tags=["Client Portal"], include_in_schema=False)
def update_my_site_content_legacy(
    payload: UpdateContentRequest,
    current_user: Annotated[dict[str, Any], Depends(get_current_user)],
    auto_publish: bool = Query(default=False),
) -> dict[str, Any]:
    site_id = require_client_tenant_id(current_user)
    return update_site_content_endpoint(site_id, payload, site={"site_id": site_id},
                                        current_user=current_user, auto_publish=auto_publish)


@app.put("/api/v1/sites/{site_id}/content/batch", tags=["Client Portal"])
def update_site_content_batch_endpoint(
    site_id: str,
    payload: BatchUpdateContentRequest,
    site: Annotated[dict[str, Any], Depends(verify_site_ownership)],
    current_user: Annotated[dict[str, Any], Depends(get_current_user)],
    auto_publish: bool = Query(default=True, description="Republier immédiatement après sauvegarde"),
) -> dict[str, Any]:
    """Sauvegarde toutes les modifications de l'éditeur visuel en une seule opération.

    ``results`` détaille chaque modification (``updated``, ``unchanged``,
    ``not_found``, ``conflict`` avec ``current_value``, ``invalid`` avec ``detail``) :
    le Studio ne considère enregistré que ce qui l'a réellement été.
    """
    try:
        results = db.update_content_values(site_id, [change.model_dump() for change in payload.changes],
                                           author=current_user.get("username"))
    except db.ContentStoreError as exc:
        logger.error("%s", exc)
        raise HTTPException(status_code=502, detail="Enregistrement momentanément impossible : réessayez.") from None
    updated_count = sum(1 for result in results if result["status"] == "updated")
    # « inchangé » = déjà enregistré (réponse perdue, nouvel essai) : la publication est
    # idempotente, on la relance pour qu'un enregistrement non publié finisse en ligne.
    saved_count = updated_count + sum(1 for result in results if result["status"] == "unchanged")
    response: dict[str, Any] = {
        "status": "success",
        "site_id": site_id,
        "items_updated": updated_count,
        "results": [{key: value for key, value in result.items() if key != "row"} for result in results],
        "published": False,
        "publish_method": None,
        "publish_detail": "",
    }
    if auto_publish and saved_count:
        response.update(_publish_fields(publish_site_content(site_id)))
    return response


@app.put("/waas/my-site/content/batch", tags=["Client Portal"], include_in_schema=False)
def update_my_site_content_batch_legacy(
    payload: BatchUpdateContentRequest,
    current_user: Annotated[dict[str, Any], Depends(get_current_user)],
    auto_publish: bool = Query(default=True),
) -> dict[str, Any]:
    site_id = require_client_tenant_id(current_user)
    return update_site_content_batch_endpoint(site_id, payload, site={"site_id": site_id},
                                              current_user=current_user, auto_publish=auto_publish)


@app.delete("/api/v1/sites/{site_id}/content", tags=["Client Portal"])
def delete_detached_site_content(
    site_id: str,
    site: Annotated[dict[str, Any], Depends(verify_site_ownership)],
    file_path: str = Query(..., min_length=1, max_length=MAX_CONTENT_PATH_LENGTH),
    xpath: str = Query(..., min_length=1, max_length=MAX_CONTENT_KEY_LENGTH * 2),
) -> dict[str, Any]:
    """Supprime un contenu **détaché** (il ne correspond plus à aucun élément de la page)."""
    if not content_sync.is_detached(xpath):
        raise HTTPException(status_code=422, detail="Seuls les contenus détachés peuvent être supprimés.")
    try:
        deleted = db.delete_detached_content(site_id, file_path, xpath)
    except db.ContentStoreError as exc:
        logger.error("%s", exc)
        raise HTTPException(status_code=502, detail="Suppression momentanément impossible : réessayez.") from None
    if not deleted:
        raise HTTPException(status_code=404, detail="Contenu introuvable.")
    return {"status": "success", "site_id": site_id, "deleted": 1}


ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/gif": ".gif",
}
MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 Mo


@app.post("/api/v1/sites/{site_id}/upload", tags=["Client Portal"])
async def upload_site_image_endpoint(
    site_id: str,
    site: Annotated[dict[str, Any], Depends(verify_site_ownership)],
    file: UploadFile = File(..., description="Fichier image (JPG, PNG, WEBP, SVG - Max 5Mo)"),
) -> dict[str, Any]:
    """Téléverse une image dans le cloud Supabase et génère une URL publique CDN."""
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Format invalide '{file.content_type}'. Formats autorisés : JPG, PNG, WEBP, SVG, GIF.",
        )

    file_bytes = await file.read()
    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="Fichier trop volumineux (limite fixée à 5 Mo).")

    public_url = await asyncio.to_thread(
        db.upload_tenant_image,
        tenant_id=site_id,
        file_bytes=file_bytes,
        file_name=file.filename or "media.png",
        content_type=file.content_type,
    )

    if not public_url:
        raise HTTPException(status_code=500, detail="Échec de l'enregistrement dans le bucket Supabase.")

    return {
        "status": "success",
        "site_id": site_id,
        "filename": file.filename,
        "public_url": public_url,
    }


@app.post("/waas/my-site/upload", tags=["Client Portal"], include_in_schema=False)
async def upload_image_legacy(
    current_user: Annotated[dict[str, Any], Depends(get_current_user)],
    file: UploadFile = File(...),
) -> dict[str, Any]:
    site_id = require_client_tenant_id(current_user)
    return await upload_site_image_endpoint(site_id, site={"site_id": site_id}, file=file)


@app.post("/api/v1/sites/{site_id}/upload-and-assign", tags=["Client Portal"])
async def upload_and_assign_site_image(
    site_id: str,
    site: Annotated[dict[str, Any], Depends(verify_site_ownership)],
    xpath: str = Query(..., max_length=MAX_CONTENT_KEY_LENGTH, description="XPath de la balise <img> à mettre à jour"),
    auto_publish: bool = Query(default=False, description="Republie la vitrine immédiatement"),
    file: UploadFile = File(..., description="Fichier image"),
    file_path: Optional[str] = Query(default=None, max_length=MAX_CONTENT_PATH_LENGTH,
                                     description="Page de l'image (recommandé)"),
) -> dict[str, Any]:
    """Upload une image ET remplace directement le src de l'élément ciblé par son XPath."""
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Format image invalide.")

    file_bytes = await file.read()
    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="Fichier trop volumineux (max 5 Mo).")

    public_url = await asyncio.to_thread(
        db.upload_tenant_image,
        tenant_id=site_id,
        file_bytes=file_bytes,
        file_name=file.filename or "media.png",
        content_type=file.content_type,
    )

    if not public_url:
        raise HTTPException(status_code=500, detail="Échec de l'upload Supabase.")

    updated_record = await asyncio.to_thread(
        db.update_single_content,
        tenant_id=site_id,
        xpath=xpath,
        new_value=public_url,
        file_path=file_path,
    )

    response: dict[str, Any] = {
        "status": "success",
        "site_id": site_id,
        "xpath": xpath,
        "new_image_url": public_url,
        "updated_record": updated_record,
        "published": False,
    }
    if auto_publish and updated_record:
        response.update(_publish_fields(await asyncio.to_thread(publish_site_content, site_id)))
    return response


@app.post("/waas/my-site/upload-and-assign", tags=["Client Portal"], include_in_schema=False)
async def upload_and_assign_image_legacy(
    current_user: Annotated[dict[str, Any], Depends(get_current_user)],
    xpath: str = Query(...),
    auto_publish: bool = Query(default=False),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    site_id = require_client_tenant_id(current_user)
    return await upload_and_assign_site_image(site_id, site={"site_id": site_id}, xpath=xpath, auto_publish=auto_publish, file=file)


@app.get("/editor", include_in_schema=False)
def get_visual_editor(
    request: Request,
    current_user: Annotated[dict[str, Any], Depends(get_current_user)],
):
    """Ancienne adresse de l'éditeur visuel : redirige vers le Studio (/studio).

    Authentification obligatoire avant toute redirection (aucune information
    divulguée à un visiteur anonyme).
    """
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=f"/studio{query}", status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@app.get("/", include_in_schema=False)
def get_home(current_user: Annotated[dict[str, Any], Depends(get_current_user)]):
    """Accueil authentifié : cockpit pour l'agence, espace client sinon."""
    target = "/admin/dashboard" if current_user.get("role") == "admin" else "/portal"
    return RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)


@app.get("/api/v1/sites/{site_id}/analytics", tags=["Client Portal"])
def get_site_analytics_endpoint(
    site_id: str,
    site: Annotated[dict[str, Any], Depends(verify_site_ownership)],
) -> dict[str, Any]:
    """Fréquentation du site (visiteurs uniques quotidiens et pages vues totales)."""
    stats = db.get_tenant_analytics(site_id)
    return {
        "site_id": site_id,
        "unique_visitors": stats["unique_visitors"],
        "total_page_views": stats["total_page_views"],
    }


@app.get("/waas/my-site/analytics", tags=["Client Portal"], include_in_schema=False)
def get_my_analytics_legacy(current_user: Annotated[dict[str, Any], Depends(get_current_user)]) -> dict[str, Any]:
    site_id = require_client_tenant_id(current_user)
    return get_site_analytics_endpoint(site_id, site={"site_id": site_id})


@app.get("/api/v1/sites/{site_id}/stats", tags=["Client Portal"])
def get_site_stats_endpoint(
    site_id: str,
    site: Annotated[dict[str, Any], Depends(verify_site_ownership)],
) -> dict[str, Any]:
    """Métriques en temps réel de consommation matérielle (CPU / RAM)."""
    if not docker_client:
        raise HTTPException(status_code=503, detail="Service Docker indisponible.")

    container_name = f"waas-{site_id}"
    try:
        container = docker_client.containers.get(container_name)
    except NotFound:
        raise HTTPException(status_code=404, detail="Site introuvable.")

    if container.status != "running":
        return {
            "site_id": site_id,
            "status": container.status,
            "cpu_percent": 0.0,
            "memory_usage_mb": 0.0,
            "memory_percent": 0.0,
        }

    try:
        raw_stats = container.stats(stream=False)
        mem_stats = raw_stats.get("memory_stats", {})
        mem_usage = mem_stats.get("usage", 0)
        mem_limit = mem_stats.get("limit", 1)

        return {
            "site_id": site_id,
            "status": container.status,
            "cpu_percent": calculate_cpu_percent(raw_stats),
            "memory_usage_mb": round(mem_usage / (1024 * 1024), 2),
            "memory_percent": round((mem_usage / mem_limit) * 100.0, 2) if mem_limit > 0 else 0.0,
        }
    except APIError as e:
        raise HTTPException(status_code=500, detail=f"Erreur télémétrie : {str(e)}")


@app.get("/waas/my-site/stats", tags=["Client Portal"], include_in_schema=False)
def get_my_site_stats_legacy(current_user: Annotated[dict[str, Any], Depends(get_current_user)]) -> dict[str, Any]:
    site_id = require_client_tenant_id(current_user)
    return get_site_stats_endpoint(site_id, site={"site_id": site_id})


@app.get("/api/v1/sites/{site_id}/logs/stream", tags=["Client Portal"])
def stream_site_logs_endpoint(
    site_id: str,
    site: Annotated[dict[str, Any], Depends(verify_site_ownership)],
    since: int | None = Query(default=None, description="Timestamp UNIX de départ (optionnel)"),
):
    """Flux SSE des logs filtré par date d'action pour la console du dashboard."""
    if not docker_client:
        raise HTTPException(status_code=503, detail="Service Docker indisponible.")

    container_name = f"waas-{site_id}"
    try:
        container = docker_client.containers.get(container_name)
    except NotFound:
        raise HTTPException(status_code=404, detail="Site introuvable.")

    def log_generator():
        valid_since = since if (since is not None and since > 0) else None

        log_stream = container.logs(
            stream=True,
            follow=True,
            since=valid_since,
            stdout=True,
            stderr=True,
        )
        try:
            for chunk in log_stream:
                line = chunk.decode("utf-8", errors="replace").rstrip()
                if line:
                    yield f"data: {line}\n\n"

                if "Démarrage de Nginx..." in line or "ready for start up" in line:
                    yield "data: [DONE] Publication terminée.\n\n"
                    break
        except GeneratorExit:
            log_stream.close()
        except Exception:
            yield "data: [Stream interrompu]\n\n"

    return StreamingResponse(
        log_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/waas/my-site/logs/stream", tags=["Client Portal"], include_in_schema=False)
def stream_my_site_logs_legacy(
    current_user: Annotated[dict[str, Any], Depends(get_current_user)],
    since: int | None = Query(default=None),
):
    site_id = require_client_tenant_id(current_user)
    return stream_site_logs_endpoint(site_id, site={"site_id": site_id}, since=since)


@app.get("/api/v1/sites/{site_id}/health", tags=["Client Portal"])
async def check_site_health_endpoint(
    site_id: str,
    site: Annotated[dict[str, Any], Depends(verify_site_ownership)],
) -> dict[str, Any]:
    """Probe complet : Conteneur local + Toutes les intégrations tierces (API, SMTP, Résas)."""
    container_info = {"status": "DOWN", "latency_ms": None, "docker_status": "unknown"}

    def _read_container_state() -> dict[str, Any]:
        """I/O Docker bloquantes : exécutées hors de l'event loop."""
        container = docker_client.containers.get(f"waas-{site_id}")
        return {"docker_status": container.status, "host_port": get_container_web_port(container)}

    if docker_client:
        try:
            state = await asyncio.to_thread(_read_container_state)
            container_status = state["docker_status"]
            host_port = state["host_port"]

            if container_status == "running" and host_port:
                # Sonde loopback dédiée : cible fixée par le code (pas de validation SSRF,
                # qui refuserait légitimement 127.0.0.1).
                local_http = await probe_local_http(
                    host_port, client=telemetry_engine.http_client, timeout=2.0
                )
                container_info = {
                    "status": local_http["status"],
                    "latency_ms": local_http["latency_ms"],
                    "docker_status": container_status,
                    "host_port": host_port,
                }
            else:
                container_info["status"] = "DOWN"
                container_info["docker_status"] = container_status
        except NotFound:
            container_info = {"status": "NOT_PROVISIONED", "docker_status": "missing"}
        except Exception as exc:
            logger.warning("Sonde locale du site %s impossible : %s", site_id, exc)
            container_info = {"status": "UNKNOWN", "latency_ms": None, "docker_status": "error"}

    try:
        monitors = await asyncio.to_thread(db.get_tenant_monitors, site_id)
    except Exception as exc:
        logger.warning("Sondes du site %s illisibles : %s", site_id, exc)
        monitors = []

    custom_results: list[dict[str, Any]] = []
    if monitors:
        raw_results = await asyncio.gather(
            *(
                execute_single_monitor(m, client=telemetry_engine.http_client)
                for m in monitors if isinstance(m, dict)
            ),
            return_exceptions=True,
        )
        for result in raw_results:
            if not isinstance(result, dict):
                # Panne interne : on expose un état dégradé sans détail technique.
                custom_results.append({
                    "id": None, "tenant_id": None, "name": None, "type": None, "target": None,
                    "status": "DOWN", "latency_ms": None, "status_code": None, "expected": None,
                    "error": "Sonde temporairement indisponible.",
                })
                continue
            # Seul le motif public sort du backend (pas d'oracle réseau interne).
            custom_results.append({
                "id": result.get("id"),
                "tenant_id": result.get("tenant_id"),
                "name": result.get("name"),
                "type": result.get("type"),
                "target": result.get("target"),
                "status": result.get("status"),
                "latency_ms": result.get("latency_ms"),
                "status_code": result.get("status_code"),
                "expected": result.get("expected"),
                "error": result.get("error_public"),
            })

    all_up = (container_info["status"] == "UP") and all(
        r.get("status") == "UP" for r in custom_results
    )
    any_down = (container_info["status"] == "DOWN") or any(
        r.get("status") == "DOWN" for r in custom_results
    )

    overall_status = "OPERATIONAL" if all_up else ("DEGRADED" if not any_down else "DOWN")

    return {
        "site_id": site_id,
        "overall_status": overall_status,
        "core_website": container_info,
        "external_services": custom_results,
    }


@app.get("/waas/my-site/health", tags=["Client Portal"], include_in_schema=False)
async def check_my_site_health_legacy(
    current_user: Annotated[dict[str, Any], Depends(get_current_user)]
) -> dict[str, Any]:
    site_id = require_client_tenant_id(current_user)
    return await check_site_health_endpoint(site_id, site={"site_id": site_id})


# =========================================================
# 3. GESTION DE FLOTTE & PARAMÈTRES (Fleet Admin Only)
# =========================================================

class SettingUpdateRequest(BaseModel):
    key: str = Field(..., pattern=r"^[a-z0-9_]{2,64}$", example="api_public_url")
    value: str = Field(..., max_length=2000, example="https://api.mondomaine.com")


@app.get("/admin/api/fleet/settings", dependencies=[Depends(require_admin)], tags=["Fleet Admin"])
@app.get("/waas/admin/settings", dependencies=[Depends(require_admin)], tags=["Fleet Admin"], include_in_schema=False)
def get_fleet_settings() -> dict[str, str]:
    """Consulte l'ensemble des réglages dynamiques de la flotte."""
    db.reload_settings_cache()
    # Copie : l'encodeur JSON ne doit jamais itérer l'objet global pendant une écriture.
    return db.get_settings_snapshot()


@app.post("/admin/api/fleet/settings", dependencies=[Depends(require_admin)], tags=["Fleet Admin"])
@app.post("/waas/admin/settings", dependencies=[Depends(require_admin)], tags=["Fleet Admin"], include_in_schema=False)
def update_fleet_setting(payload: SettingUpdateRequest) -> dict[str, Any]:
    """Modifie à chaud un paramètre (URL d'API, domaine hôte, image Docker)."""
    db.set_setting(payload.key, payload.value.strip())
    _invalidate_cache()
    return {
        "status": "success",
        "updated": {payload.key: payload.value.strip()},
    }


class SwitchModeRequest(BaseModel):
    mode: Literal["production", "local"] = Field(..., example="production")
    base_domain: Optional[str] = Field(None, example="29th.site")
    landing_site_slug: Optional[str] = Field(None, example="dwadaw")
    rebuild_containers: bool = Field(False, description="Reconstruire les conteneurs locataires actifs")


def _update_env_file(filepath: Path, updates: dict[str, str]) -> None:
    """Met à jour un fichier .env de manière sûre et atomique."""
    if not filepath.exists():
        return
    try:
        content = filepath.read_text(encoding="utf-8")
        lines = content.splitlines()
        new_lines: list[str] = []
        seen: set[str] = set()
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in updates:
                    new_lines.append(f"{key}={updates[key]}")
                    seen.add(key)
                    continue
            new_lines.append(line)
        for k, v in updates.items():
            if k not in seen:
                new_lines.append(f"{k}={v}")
        filepath.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    except Exception as exc:
        logger.warning("Échec de mise à jour du fichier d'environnement %s : %s", filepath, exc)


@app.post("/admin/api/fleet/switch-mode", dependencies=[Depends(require_admin)], tags=["Fleet Admin"])
def switch_platform_mode(payload: SwitchModeRequest) -> dict[str, Any]:
    """Bascule la plateforme entre le mode Local et le mode Production en une seule action.

    Met à jour simultanément :
    - le domaine racine des vitrines (client_base_domain) ;
    - l'URL d'API publique pour les conteneurs (api_public_url) ;
    - l'URL d'API vue du navigateur pour l'audience et le Studio (browser_api_url) ;
    - le site vitrine officiel servi sur l'apex (landing_site_slug) ;
    - les fichiers de configuration système (/etc/waas/api.env et End/.env) ;
    - et relance optionnellement la reconstruction des conteneurs actifs.
    """
    rebuilt_sites: list[str] = []
    errors: list[str] = []

    if payload.mode == "production":
        raw_domain = payload.base_domain or db.get_setting("client_base_domain", "")
        if not raw_domain:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Un nom de domaine racine est requis pour passer en mode production (ex. '29th.site').",
            )
        try:
            base_domain = normalize_base_domain(raw_domain)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from None

        landing_slug = (payload.landing_site_slug if payload.landing_site_slug is not None else db.get_setting("landing_site_slug", "")) or ""
        landing_slug = landing_slug.strip().lower()

        db.set_setting("client_base_domain", base_domain)
        # L'API pour les conteneurs locataires passe par le pont Docker hôte (ultra-rapide, zéro DNS, sans dépendre de Cloudflare WAF)
        db.set_setting("api_public_url", "http://host.docker.internal:8000")
        db.set_setting("browser_api_url", f"https://api.{base_domain}")
        if landing_slug:
            db.set_setting("landing_site_slug", landing_slug)

        env_updates = {
            "CLIENT_BASE_DOMAIN": base_domain,
            "API_PUBLIC_URL": "http://host.docker.internal:8000",
            "BROWSER_API_URL": f"https://api.{base_domain}",
        }
    else:
        # Mode Local
        base_domain = "localhost"
        db.set_setting("client_base_domain", "localhost")
        db.set_setting("api_public_url", "http://172.17.0.1:8000")
        db.set_setting("browser_api_url", "http://localhost:8000")
        db.set_setting("landing_site_slug", "")

        env_updates = {
            "CLIENT_BASE_DOMAIN": "localhost",
            "API_PUBLIC_URL": "http://172.17.0.1:8000",
            "BROWSER_API_URL": "http://localhost:8000",
        }

    # Mise à jour des fichiers d'environnement locaux
    for env_path in (Path("/etc/waas/api.env"), Path(__file__).resolve().parent / ".env"):
        _update_env_file(env_path, env_updates)

    _invalidate_cache()
    invalidate_port_cache()
    db.reload_settings_cache()

    if payload.rebuild_containers and docker_client:
        try:
            containers = docker_client.containers.list(all=False, filters={"label": "service=waas-tenant"})
            for c in containers:
                s_id = (c.labels or {}).get("tenant_id")
                if s_id:
                    try:
                        rebuild_site(s_id)
                        rebuilt_sites.append(s_id)
                    except Exception as exc:
                        errors.append(f"{s_id}: {exc}")
        except Exception as exc:
            errors.append(f"Erreur d'inventaire Docker : {exc}")

    return {
        "status": "success",
        "mode": payload.mode,
        "environment": environment_info(),
        "rebuilt": rebuilt_sites,
        "errors": errors if errors else None,
    }


# ---------------------------------------------------------
# 3.1 CACHE COURT DES LECTURES PARTAGÉES PAR UNE MÊME PAGE
# ---------------------------------------------------------
# Une page du cockpit appelle 2 à 3 endpoints en parallèle (coque + données) :
# les fiches sites et les comptes sont relus au plus toutes les 4 secondes.

_READ_CACHE: dict[str, tuple[float, Any]] = {}
_READ_CACHE_LOCK = threading.Lock()
_READ_CACHE_TTL = 4.0


#: Incrémenté à chaque invalidation : une lecture commencée **avant** une écriture ne
#: doit pas réinstaller sa valeur périmée dans le cache une fois l'écriture faite.
_READ_CACHE_GENERATION = [0]


def _cached_read(key: str, loader: Any) -> Any:
    now = time.monotonic()
    with _READ_CACHE_LOCK:
        hit = _READ_CACHE.get(key)
        if hit and now - hit[0] < _READ_CACHE_TTL:
            return hit[1]
        generation = _READ_CACHE_GENERATION[0]
    value = loader()
    with _READ_CACHE_LOCK:
        if generation == _READ_CACHE_GENERATION[0]:
            _READ_CACHE[key] = (now, value)
    return value


def _invalidate_cache() -> None:
    with _READ_CACHE_LOCK:
        _READ_CACHE.clear()
        _READ_CACHE_GENERATION[0] += 1


def site_records() -> list[dict[str, Any]]:
    return _cached_read("site_records", db.list_site_records)


def all_clients() -> list[dict[str, Any]]:
    return _cached_read("clients", db.get_all_clients)


def site_public_url(record: Optional[dict[str, Any]], port: Optional[str]) -> Optional[str]:
    """URL publique : domaine racine/sous-domaine en production, sinon hôte:port configuré."""
    base_domain = clean_domain(db.get_setting("client_base_domain", ""))
    is_prod = is_production_mode() and bool(base_domain and base_domain not in ("localhost", "127.0.0.1"))

    if is_prod:
        site_id = str((record or {}).get("site_id") or (record or {}).get("id") or "").strip()
        domain = str((record or {}).get("domain") or "").strip()
        landing_slug = (db.get_setting("landing_site_slug", "") or "").strip().lower()

        # 1. Site vitrine officiel configuré sur l'apex (ex. 29th.site)
        if landing_slug and site_id.lower() == landing_slug:
            return f"https://{base_domain}"
        if domain and clean_domain(domain) in (base_domain, f"www.{base_domain}"):
            return f"https://{base_domain}"

        # 2. Domaine personnalisé explicite propre au client (ex. https://boutique-client.com)
        if domain:
            cleaned_d = clean_domain(domain)
            if cleaned_d not in (base_domain, f"www.{base_domain}", f"{site_id}.{base_domain}"):
                return domain if "://" in domain else f"https://{cleaned_d}"

        # 3. Sous-domaine locataire standard (ex. https://dwadaw.29th.site)
        if site_id:
            return f"https://{site_id}.{base_domain}"

    domain = str((record or {}).get("domain") or "").strip()
    if domain and is_production_mode():
        return domain if "://" in domain else f"https://{domain}"

    return build_site_url(port)


def fresh_inventory() -> list[dict[str, Any]]:
    """État Docker **actuel** des conteneurs (le statut ne doit jamais être périmé après
    une action), complété par les mesures du dernier cycle de télémétrie."""
    cached = {item["site_id"]: item for item in telemetry_engine.get_inventory()}
    if not docker_client:
        return list(cached.values())
    try:
        containers = docker_client.containers.list(all=True, filters={"label": "service=waas-tenant"})
    except (APIError, DockerException) as exc:
        logger.warning("Inventaire Docker indisponible, cache de télémétrie utilisé : %s", exc)
        return list(cached.values())
    inventory = []
    for container in containers:
        info = describe_container(container)
        previous = cached.get(info["site_id"], {})
        info["disk_mb"] = previous.get("disk_mb")
        if info["running"]:
            info.update({key: previous.get(key) for key in ("cpu_percent", "memory_mb", "pids")})
        else:
            info.update(cpu_percent=None, memory_mb=None, pids=None)
        inventory.append(info)
    return inventory


def load_catalog(inventory: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
    """Catalogue fusionné (fiches `sites` × conteneurs × visiteurs en direct)."""
    entries = cockpit.build_catalog(
        site_records(),
        fresh_inventory() if inventory is None else inventory,
        all_clients(),
        telemetry_engine.get_live_by_tenant(),
        site_public_url,
    )
    for entry in entries:   # compatibilité des anciens écrans (/waas/admin/list)
        entry.update(id=entry["container_id"], tenant_id=entry["site_id"], is_active=entry["running"])
    return entries


def find_site_entry(site_id: str) -> Optional[dict[str, Any]]:
    return next((entry for entry in load_catalog() if entry["site_id"] == site_id), None)


def security_warnings() -> list[str]:
    warnings = []
    if SECRET_KEY == _DEFAULT_SECRET_KEY:
        warnings.append("WAAS_SECRET_KEY utilise la valeur par défaut : les sessions sont falsifiables.")
    if ANALYTICS_PEPPER == _DEFAULT_PEPPER:
        warnings.append("WAAS_ANALYTICS_PEPPER utilise la valeur par défaut.")
    if sync_auth.uses_default_secret():
        warnings.append("WAAS_SYNC_SECRET absent : jetons de synchronisation dérivés de la clé JWT.")
    if DOCS_ENABLED and is_production_mode():
        warnings.append("Documentation /docs exposée en production (WAAS_ENABLE_DOCS=0 recommandé).")
    return warnings


def current_incidents(catalog: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
    catalog = catalog if catalog is not None else load_catalog(inventory=telemetry_engine.get_inventory())
    cockpit_tables = {db.TABLE_SITES, db.TABLE_INVOICES, db.TABLE_TICKETS, db.TABLE_TICKET_MESSAGES,
                      db.TABLE_UPTIME, db.TABLE_HOST_HOURLY}
    missing = [relation for relation in db.missing_relations() if relation in cockpit_tables]
    return cockpit.build_incidents(
        telemetry_engine.get_host(), telemetry_engine.get_fleet_health(), catalog,
        schema_missing=missing, security_warnings=security_warnings(),
    )


# ---------------------------------------------------------
# 3.2 COMPTES CLIENTS
# ---------------------------------------------------------

class CockpitModel(BaseModel):
    """Corps de requête du cockpit : espaces de tête/fin retirés avant validation
    (un texte fait uniquement d'espaces échoue ainsi à ``min_length``)."""

    model_config = ConfigDict(str_strip_whitespace=True)


USERNAME_PATTERN = r"^[a-z0-9._@-]{3,64}$"
MIN_PASSWORD_LENGTH = 10


class ClientCreateRequest(BaseModel):
    username: str = Field(..., pattern=USERNAME_PATTERN, description="Identifiant de connexion (minuscules)")
    password: str = Field(..., min_length=MIN_PASSWORD_LENGTH, max_length=128)
    tenant_id: Optional[str] = Field(default=None, pattern=r"^[a-z0-9-]*$", max_length=64)


class ClientPasswordRequest(BaseModel):
    password: str = Field(..., min_length=MIN_PASSWORD_LENGTH, max_length=128)


class ClientUpdateRequest(CockpitModel):
    tenant_id: Optional[str] = Field(default=None, pattern=r"^[a-z0-9-]*$", max_length=64)


def _require_client(client_id: str) -> dict[str, Any]:
    user = db.get_user_by_id(client_id)
    if not user or user.get("role") != "client":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Compte client introuvable.")
    return user


@app.get("/admin/api/clients", dependencies=[Depends(require_admin)], tags=["Fleet Admin"])
def list_all_clients() -> dict[str, Any]:
    """Liste tous les comptes clients avec leurs sites rattachés."""
    clients = db.get_all_clients()
    live = telemetry_engine.get_live_by_tenant()
    for client in clients:
        client["live_visitors"] = sum(live.get(str(s.get("site_id") or s.get("id")), 0) for s in client.get("sites") or [])
    return {
        "count": len(clients),
        "clients": clients,
    }


@app.post("/admin/api/clients", dependencies=[Depends(require_admin)], tags=["Fleet Admin"], status_code=201)
def create_client_account(payload: ClientCreateRequest) -> dict[str, Any]:
    """Crée un compte client (mot de passe haché par la base, jamais stocké en clair)."""
    if db.get_user_by_username(payload.username):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cet identifiant est déjà utilisé.")
    user = db.create_user(payload.username, payload.password, role="client", tenant_id=payload.tenant_id or None)
    if payload.tenant_id:
        assign_site_owner(payload.tenant_id, str(user["id"]))
    _invalidate_cache()
    return {"status": "success", "client": user}


@app.patch("/admin/api/clients/{client_id}", dependencies=[Depends(require_admin)], tags=["Fleet Admin"])
def update_client_account(client_id: str, payload: ClientUpdateRequest) -> dict[str, Any]:
    _require_client(client_id)
    updated = db.update_user_tenant(client_id, payload.tenant_id or None)
    if payload.tenant_id:
        assign_site_owner(payload.tenant_id, client_id)
    _invalidate_cache()
    return {"status": "success", "client": updated}


def assign_site_owner(site_id: str, client_id: Optional[str]) -> None:
    """Change le propriétaire d'un site : la fiche fait foi, les anciens liens sont effacés.

    Sans table ``sites`` (migration non appliquée), ``waas_users.tenant_id`` reste la
    seule source : effacer les liens des autres comptes garantit un propriétaire unique.
    """
    with_suppressed_schema(lambda: db.upsert_site_record(site_id, {"client_id": client_id}))
    try:
        db.clear_tenant_refs(site_id, keep_user_id=client_id)
    except Exception as exc:  # hygiène : la fiche fait déjà foi
        logger.warning("Nettoyage des rattachements tenant_id de '%s' impossible : %s", site_id, exc)


@app.post("/admin/api/clients/{client_id}/password", dependencies=[Depends(require_admin)], tags=["Fleet Admin"])
def reset_client_password(client_id: str, payload: ClientPasswordRequest) -> dict[str, Any]:
    _require_client(client_id)
    if not db.set_user_password(client_id, payload.password):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Compte client introuvable.")
    return {"status": "success"}


@app.delete("/admin/api/clients/{client_id}", dependencies=[Depends(require_admin)], tags=["Fleet Admin"])
def delete_client_account(client_id: str) -> dict[str, Any]:
    """Supprime un compte client ; ses sites restent en place, détachés (client_id -> NULL)."""
    _require_client(client_id)
    if not db.delete_user(client_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Compte client introuvable.")
    _invalidate_cache()
    return {"status": "success", "deleted": client_id}


def with_suppressed_schema(action: Any) -> Any:
    """Écriture annexe facultative : ignorée si la table n'existe pas encore."""
    try:
        return action()
    except db.SchemaNotReady as exc:
        logger.info("Écriture annexe ignorée : %s", exc)
        return None


# ---------------------------------------------------------
# 3.3 CATALOGUE DES SITES (FICHE COMMERCIALE × CONTENEUR)
# ---------------------------------------------------------

class ContainerStatusFilter(str, Enum):
    ALL = "all"
    ACTIVE = "active"
    INACTIVE = "inactive"


@app.get("/admin/api/sites", dependencies=[Depends(require_admin)], tags=["Fleet Admin"])
@app.get("/waas/admin/list", dependencies=[Depends(require_admin)], tags=["Fleet Admin"], include_in_schema=False)
def list_all_containers(
    status: ContainerStatusFilter = Query(
        default=ContainerStatusFilter.ALL,
        description="Filtre d'état : 'active', 'inactive', ou 'all'",
    )
) -> dict[str, Any]:
    """Catalogue de la flotte : chaque site connu (fiche et/ou conteneur), état Docker actuel."""
    results = load_catalog()
    if status == ContainerStatusFilter.ACTIVE:
        results = [s for s in results if s["running"]]
    elif status == ContainerStatusFilter.INACTIVE:
        results = [s for s in results if not s["running"]]

    return {
        "count": len(results),
        "filter_applied": status.value,
        "docker_available": docker_client is not None,
        "counts": cockpit.fleet_counts(results),
        "containers": results,
        "plans": list(cockpit.PLANS),
        "private_repos": git_source.private_repos_enabled(),
    }


def validate_container_api_url(api_url: str) -> str:
    """L'URL de l'API doit être **joignable depuis un conteneur client**.

    Depuis un conteneur, ``localhost`` désigne le conteneur lui-même : une valeur
    ``http://localhost:8000`` produit un conteneur qui redémarre en boucle (le
    moteur de synchronisation ne peut pas joindre l'API). On refuse donc la
    création plutôt que de laisser un crashloop silencieux s'installer.
    """
    value = (api_url or "").strip().rstrip("/")
    host = urlparse(value if "://" in value else f"//{value}").hostname if value else None
    if not value or not host:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Réglage 'api_public_url' absent : les conteneurs clients doivent pouvoir "
                "joindre l'API de synchronisation. Définissez une URL publique "
                "(ex. https://api.exemple.com) puis relancez le déploiement."
            ),
        )
    if host.lower() in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"'api_public_url' = '{value}' est injoignable depuis un conteneur client "
                "(localhost y désigne le conteneur). Utilisez une URL publique, ou "
                "http://host.docker.internal:<port> après avoir lié l'API sur une interface "
                "accessible aux conteneurs (jamais 127.0.0.1 seul)."
            ),
        )
    return value


def build_tenant_environment(tenant_id: str, repo_url: str, api_url: str) -> dict[str, str]:
    """Environnement d'un conteneur locataire : **aucun secret du plan de contrôle**.

    Le conteneur n'obtient ni clé Supabase ``service_role`` ni accès direct à la
    base : il reçoit un jeton HMAC scopé à son propre ``tenant_id`` et passe par
    les endpoints internes de synchronisation de l'API.

    Note : contrairement à `docker.from_env()`, l'environnement est pensé pour un
    runtime non fiable — tout ce qui y figure doit être considéré comme public.
    Un dépôt privé ne fait donc **jamais** entrer le jeton GitHub ici :
    ``SOURCE_MODE=api`` indique au conteneur de demander l'archive à l'API, avec
    ``SOURCE_NONCE``, clé aléatoire propre à **ce** conteneur.
    """
    mode = git_source.source_mode(repo_url)
    environment = {
        "TENANT_ID": tenant_id,
        "REPO_URL": repo_url,
        "SOURCE_MODE": mode,
        "API_BASE_URL": api_url,
        "API_LOCAL_FALLBACK": "http://host.docker.internal:8000",
        "SYNC_TOKEN": sync_auth.issue_tenant_token(tenant_id),
    }
    if mode == git_source.SOURCE_MODE_API:
        environment["SOURCE_NONCE"] = git_source.new_source_nonce()
    return environment


TENANT_NETWORK_NAME = "waas-tenants"
DEFAULT_ALLOWED_GIT_HOSTS = ("github.com", "gitlab.com")

#: Contenu du site : volume anonyme, seul point d'écriture d'un conteneur dont le
#: rootfs est monté en lecture seule.
TENANT_CONTENT_PATH = "/var/www/html"
TENANT_TMPFS = {
    "/tmp": "rw,noexec,nosuid,size=128m,mode=1777",
    "/run": "rw,noexec,nosuid,size=8m,mode=1777",
}
TENANT_MEM_LIMIT = "256m"
TENANT_NANO_CPUS = 500_000_000      # 0,5 vCPU (unité : 1e-9 CPU)
TENANT_PIDS_LIMIT = 128


def allowed_git_hosts() -> tuple[str, ...]:
    """Hôtes Git autorisés (réglage ``allowed_git_hosts``, sinon valeurs par défaut)."""
    raw = db.get_setting("allowed_git_hosts", "") or ""
    hosts = tuple(host.strip().lower() for host in raw.split(",") if host.strip())
    return hosts or DEFAULT_ALLOWED_GIT_HOSTS


def validate_repo_url(raw_url: str) -> str:
    """N'accepte qu'un dépôt https sur un hôte autorisé, sans identifiants.

    Sans cette liste blanche, ``git clone`` exécuté dans le conteneur transforme le
    déploiement en SSRF avec écriture de fichier : le corps de la réponse (métadonnées
    cloud, service interne) est cloné dans la racine web puis **servi publiquement**.
    """
    parsed = urlparse(raw_url)
    if parsed.scheme != "https":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="URL de dépôt invalide : le schéma https est obligatoire.",
        )
    if parsed.username or parsed.password:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Identifiants interdits dans l'URL du dépôt (ils seraient publiés).",
        )
    host = (parsed.hostname or "").lower()
    hosts = allowed_git_hosts()
    if host not in hosts:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Hôte Git non autorisé : '{host or 'inconnu'}'. Autorisés : {', '.join(hosts)}.",
        )
    try:
        git_source.github_repository(raw_url)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    return raw_url


def check_repo_access(repo_url: str) -> None:
    """Refuse (422) un dépôt **certainement** illisible : privé sans jeton, hors de
    portée du jeton, jeton expiré. Sans ce contrôle, l'erreur n'apparaissait qu'en
    redémarrages en boucle du conteneur. Appelant : ``validate_repo_url`` déjà passé."""
    error = git_source.repo_access_error(repo_url)
    if error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=error)


def ensure_tenant_network(client: Any) -> str:
    """Réseau Docker dédié aux locataires, sans communication entre conteneurs."""
    try:
        client.networks.get(TENANT_NETWORK_NAME)
        return TENANT_NETWORK_NAME
    except NotFound:
        pass
    except APIError as exc:
        logger.warning("Réseau %s illisible (%s) : bridge par défaut utilisé.", TENANT_NETWORK_NAME, exc)
        return "bridge"

    try:
        client.networks.create(
            TENANT_NETWORK_NAME,
            driver="bridge",
            options={"com.docker.network.bridge.enable_icc": "false"},
            labels={"service": "waas-tenant-network"},
        )
        logger.info("Réseau Docker '%s' créé (communication inter-conteneurs désactivée).", TENANT_NETWORK_NAME)
        return TENANT_NETWORK_NAME
    except APIError as exc:
        logger.warning("Création du réseau %s impossible (%s) : bridge par défaut utilisé.", TENANT_NETWORK_NAME, exc)
        return "bridge"


def validate_browser_api_url(browser_url: str) -> str:
    """L'URL « vue du navigateur » doit être une origine http(s) exploitable.

    Contrairement à ``validate_container_api_url``, ``localhost`` y est **normal**
    (le navigateur tourne sur l'hôte). On refuse en revanche tout ce qui casserait
    ses deux consommateurs : schéma absent ou exotique (``javascript:``), dépôt
    d'identifiants, port illisible, et caractères qui s'échapperaient dans
    l'en-tête ``frame-ancestors`` du serveur des vitrines. Un hôte nu
    (``localhost:8000``) est accepté et normalisé en URL absolue : l'endpoint du
    micro-tracker doit toujours être absolu, sinon le navigateur le résout contre
    l'origine de la vitrine et la collecte retombe en silence.
    """
    value = (browser_url or "").strip().rstrip("/")
    if re.search(r"[\s\"<>`\\]", value):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "'browser_api_url' contient des espaces ou des caractères interdits "
                "(guillemets, chevrons) : cette valeur finit dans l'en-tête "
                "frame-ancestors du serveur des vitrines."
            ),
        )
    parsed = urlparse(value if "://" in value else f"http://{value}")
    try:
        port = parsed.port
    except ValueError:
        port = None
    authority = f"{parsed.hostname or ''}{f':{port}' if port else ''}"
    if (not value or parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.netloc.lower() != authority.lower()):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Réglage 'browser_api_url' = '{value}' invalide : indiquez l'URL de l'API "
                "telle que votre navigateur la joint (ex. http://localhost:8000 ou "
                "https://api.exemple.com), ou laissez vide pour reprendre 'api_public_url'."
            ),
        )
    if not re.fullmatch(r"[A-Za-z0-9.\-]+", parsed.hostname):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"'browser_api_url' = '{value}' : nom d'hôte invalide.",
        )
    return f"{parsed.scheme}://{authority}"


def portal_frame_ancestors(browser_url: str) -> str:
    """Valeur de ``frame-ancestors`` des vitrines : origine(s) du Studio.

    Le Studio est servi par l'API : seule l'origine de l'API **vue du navigateur**
    doit pouvoir encadrer une vitrine. Les alias de bouclage ``localhost`` /
    ``127.0.0.1`` sont ajoutés lorsque l'un des deux est configuré : ce sont deux
    origines distinctes pour un navigateur, et un administrateur passe de l'une à
    l'autre sans y penser (symptôme sinon : « Firefox ne peut pas ouvrir cette
    page » sur l'aperçu, sans autre explication).
    """
    parsed = urlparse(browser_url if "://" in browser_url else f"http://{browser_url}")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "'self'"
    port = f":{parsed.port}" if parsed.port else ""
    host = parsed.hostname.lower()
    origins = [f"{parsed.scheme}://{host}{port}"]
    if host in ("localhost", "127.0.0.1"):
        for alias in ("localhost", "127.0.0.1"):
            if alias != host:
                origins.append(f"{parsed.scheme}://{alias}{port}")
    return " ".join(origins)


def tenant_port_binding() -> str:
    """Interface d'écoute du port des vitrines.

    Par défaut ``127.0.0.1`` : le site n'est joignable que par le reverse-proxy de
    l'hôte (TLS, WAF, limitation de débit). La valeur ``0.0.0.0`` (exposition directe
    sur Internet, sans TLS) reste possible via le réglage ``tenant_port_binding``
    pour ne pas casser une architecture existante, mais elle est déconseillée.
    """
    value = (db.get_setting("tenant_port_binding", "127.0.0.1") or "127.0.0.1").strip()
    if value not in ("127.0.0.1", "0.0.0.0"):
        logger.warning("Réglage tenant_port_binding invalide ('%s') : 127.0.0.1 utilisé.", value)
        return "127.0.0.1"
    if value == "0.0.0.0":
        logger.warning(
            "tenant_port_binding=0.0.0.0 : les vitrines sont publiées sur toutes les interfaces, "
            "sans TLS ni reverse-proxy. À réserver au dépannage."
        )
    return value


class DeployTenantRequest(CockpitModel):
    site_id: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9-]+$",
        description="Identifiant unique du site (minuscules, chiffres et tirets)",
        example="boutique-dupont",
    )
    tenant_id: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9-]+$",
        description="Identifiant unique du site (rétrocompatibilité tenant_id)",
        example="boutique-dupont",
    )
    client_id: str | None = Field(
        default=None,
        description="ID du compte client auquel rattacher ce site (optionnel)",
        example="dupont",
    )
    github_url: HttpUrl = Field(
        ...,
        description="URL du dépôt du template (GitHub privé accepté si WAAS_GITHUB_TOKEN est défini)",
        example="https://github.com/awddawawd/template_webpage.git",
    )
    # Fiche commerciale (facultative) : enregistrée dans la table `sites` si elle existe.
    name: str | None = Field(default=None, max_length=120, description="Nom commercial de la vitrine")
    plan_id: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,32}$", description="Formule (none, starter…)")
    price_monthly: float | None = Field(default=None, ge=0, le=100000, description="Prix mensuel (€)")
    domain: str | None = Field(default=None, max_length=253, description="Domaine externe (optionnel)")


DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)([a-z0-9-]{1,63}\.)+[a-z]{2,63}$")


def normalize_domain(value: Optional[str]) -> Optional[str]:
    """Nom de domaine nu (sans schéma ni chemin) validé, ou ``None`` si vide."""
    raw = (value or "").strip().lower()
    if not raw:
        return None
    host = cockpit.url_host(raw) or raw
    if not DOMAIN_RE.match(host):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"Nom de domaine invalide : '{raw}'.")
    return host


def plan_fields(plan_id: Optional[str], price: Optional[float]) -> dict[str, Any]:
    """Formule connue -> libellé et prix catalogue (prix personnalisé prioritaire)."""
    if not plan_id:
        return {} if price is None else {"price_monthly": round(price, 2)}
    plan = cockpit.PLANS_BY_ID.get(plan_id)
    fields: dict[str, Any] = {"plan_id": plan_id, "plan_name": plan["name"] if plan else plan_id}
    fields["price_monthly"] = round(price if price is not None else (plan or {}).get("price", 0.0), 2)
    return fields


def provision_container(site_id: str, repo_url: str) -> Any:
    """Crée et démarre le conteneur durci d'une vitrine (appelant : validations faites)."""
    base_image = db.get_setting("docker_base_image", "waas-base-image:latest")
    # Refuse une URL d'API que les conteneurs ne pourraient pas joindre (fail-fast).
    api_url = validate_container_api_url(api_public_url_setting())
    # URL vue du navigateur : elle est **gravée dans le HTML** des vitrines
    # (endpoint du micro-tracker) et dans frame-ancestors. Une valeur fausse ici
    # casse l'audience et l'aperçu du Studio sans autre signal.
    browser_url = validate_browser_api_url(browser_api_url_setting())
    if urlparse(browser_url if "://" in browser_url else f"http://{browser_url}").hostname in (
        "host.docker.internal", "gateway.docker.internal",
    ):
        logger.warning(
            "URL de l'API vue du navigateur = '%s' (%s) : cet hôte ne résout que depuis un "
            "conteneur. La collecte d'audience (sendBeacon) et l'aperçu encadré du Studio "
            "resteront muets dans le navigateur. Renseignez le réglage 'browser_api_url' "
            "(ex. http://localhost:8000 en local) puis recréez la vitrine.",
            browser_url,
            "réglage 'browser_api_url'" if browser_api_url_raw()
            else "'browser_api_url' absent, repli sur 'api_public_url'",
        )
    container_name = f"waas-{site_id}"
    network = ensure_tenant_network(docker_client)
    try:
        container = _run_tenant_container(base_image, container_name, site_id, repo_url, api_url,
                                          browser_url, network)
    except APIError:
        # `run` = create + start : un démarrage refusé laisse un conteneur « Created »
        # qui bloquerait tout nouvel essai (« existe déjà »).
        discard_container(container_name)
        raise
    container.reload()
    return container


def discard_container(container_name: str) -> None:
    try:
        docker_client.containers.get(container_name).remove(force=True, v=True)
    except NotFound:
        pass
    except APIError as exc:
        logger.error("Conteneur partiel '%s' non supprimé : %s", container_name, exc)


def container_environment(container: Any) -> dict[str, str]:
    """Environnement **provisionné** d'un conteneur (fixé à sa création, non modifiable par lui)."""
    entries = (container.attrs.get("Config") or {}).get("Env") or []
    return dict(entry.split("=", 1) for entry in entries if "=" in entry)


def _run_tenant_container(base_image: str, container_name: str, site_id: str, repo_url: str,
                          api_url: str, browser_url: str, network: Any) -> Any:
    return docker_client.containers.run(
        image=base_image,
        name=container_name,
        detach=True,
        # --- Durcissement du runtime locataire ---------------------------
        # Contenu semi-tiers exécuté sans privilèges, sans capabilities, sans
        # écriture hors du volume de contenu, avec des plafonds de ressources.
        user="10001:10001",
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        read_only=True,
        tmpfs=dict(TENANT_TMPFS),
        # Volume **anonyme** (source=None) pour le contenu : `volumes={path: {}}` exige un
        # montage hôte (`bind`) et lève KeyError. Docker l'initialise depuis l'image
        # (fichiers et propriétaire 10001) ; `remove(v=True)` le supprime avec le conteneur.
        mounts=[Mount(target=TENANT_CONTENT_PATH, source=None, type="volume")],
        mem_limit=TENANT_MEM_LIMIT,
        memswap_limit=TENANT_MEM_LIMIT,
        nano_cpus=TENANT_NANO_CPUS,
        pids_limit=TENANT_PIDS_LIMIT,
        ulimits=[
            Ulimit(name="nofile", soft=1024, hard=2048),
            Ulimit(name="nproc", soft=128, hard=256),
        ],
        log_config={"Type": "json-file", "Config": {"max-size": "10m", "max-file": "3"}},
        healthcheck={
            "test": ["CMD-SHELL", "wget -q -O /dev/null http://127.0.0.1:8080/ || exit 1"],
            # Durées Docker en **nanosecondes** : 30 s, 3 s, 45 s.
            "interval": 30 * 1_000_000_000,
            "timeout": 3 * 1_000_000_000,
            "retries": 3,
            "start_period": 45 * 1_000_000_000,
        },
        # -----------------------------------------------------------------
        restart_policy={"Name": "unless-stopped"},
        # Jamais 0.0.0.0 par défaut : le site reste derrière le reverse-proxy.
        ports={"8080/tcp": (tenant_port_binding(), None)},
        network=network,
        environment={
            **build_tenant_environment(tenant_id=site_id, repo_url=repo_url, api_url=api_url),
            # Seule origine autorisée à encadrer la vitrine : l'aperçu du Studio.
            "PORTAL_ORIGIN": (
                f"{portal_frame_ancestors(browser_url)} https://app.{clean_domain(db.get_setting('client_base_domain', ''))}"
                if (is_production_mode() and clean_domain(db.get_setting('client_base_domain', '')) not in ('localhost', '127.0.0.1'))
                else portal_frame_ancestors(browser_url)
            ),
            # Endpoint du micro-tracker injecté dans le HTML : il est appelé par le
            # **navigateur du visiteur**, jamais par le conteneur.
            "BROWSER_API_URL": browser_url,
        },
        # Permet d'utiliser http://host.docker.internal:<port> même sous Linux.
        extra_hosts={"host.docker.internal": "host-gateway"},
        labels={
            "service": "waas-tenant",
            "tenant.id": site_id,
        },
    )


@app.post("/admin/api/sites/deploy", dependencies=[Depends(require_admin)], tags=["Fleet Admin"])
@app.post("/waas/admin/deploy", dependencies=[Depends(require_admin)], tags=["Fleet Admin"], include_in_schema=False)
def deploy_tenant(payload: DeployTenantRequest) -> dict[str, Any]:
    if not docker_client:
        raise HTTPException(status_code=503, detail="Daemon Docker inaccessible.")

    target_site_id = payload.site_id or payload.tenant_id
    if not target_site_id:
        raise HTTPException(status_code=422, detail="Veuillez spécifier 'site_id' ou 'tenant_id'.")

    container_name = f"waas-{target_site_id}"
    action_timestamp = int(time.time())

    existing = docker_client.containers.list(all=True, filters={"name": f"^{container_name}$"})
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Le conteneur '{container_name}' existe déjà.",
        )

    # Refuse une URL d'API que les conteneurs ne pourraient pas joindre (fail-fast).
    validate_container_api_url(api_public_url_setting())
    # Refuse un dépôt hors liste blanche / avec identifiants (SSRF + fuite de jeton).
    repo_url = validate_repo_url(str(payload.github_url))
    domain = normalize_domain(payload.domain)
    # Compte vérifié **avant** de créer le conteneur : pas de vitrine orpheline sur erreur.
    if payload.client_id:
        _require_client(payload.client_id)
    # Dernier contrôle (réseau) : dépôt privé inaccessible -> 422 plutôt qu'un crashloop.
    check_repo_access(repo_url)

    try:
        container = provision_container(target_site_id, repo_url)
        host_port = get_container_web_port(container)
        if not host_port:
            raise HTTPException(status_code=500, detail="Impossible d'allouer un port hôte.")

        record_fields: dict[str, Any] = {
            "name": (payload.name or "").strip() or target_site_id,
            "repo_url": repo_url,
            **plan_fields(payload.plan_id, payload.price_monthly),
        }
        if domain:
            record_fields["domain"] = domain
        if payload.client_id:
            record_fields["client_id"] = payload.client_id
        record = save_deployed_record(target_site_id, record_fields, payload.client_id)
        _invalidate_cache()

        return {
            "status": "success",
            "site_id": target_site_id,
            "tenant_id": target_site_id,
            "client_id": payload.client_id,
            "container_id": container.short_id,
            "action_timestamp": action_timestamp,
            "host_port": host_port,
            "url": build_site_url(host_port),
            "record_saved": record is not None,
        }

    except APIError as e:
        raise HTTPException(status_code=500, detail=f"Erreur Docker : {str(e)}")


def save_deployed_record(site_id: str, fields: dict[str, Any], client_id: Optional[str]) -> Optional[dict[str, Any]]:
    """Fiche commerciale d'un site tout juste provisionné — **au mieux**.

    Le conteneur existe déjà : un échec d'écriture ne doit pas se traduire par une
    erreur 500 qui ferait croire à un déploiement raté (et à un nouvel essai en
    conflit). La réponse l'indique via ``record_saved``.
    """
    try:
        record = with_suppressed_schema(lambda: db.upsert_site_record(site_id, fields))
        if client_id:
            if record is None:  # migration absente : ancien rattachement tenant_id
                db.assign_site_to_client(site_id, client_id)
            db.clear_tenant_refs(site_id, keep_user_id=client_id)
        return record
    except Exception as exc:
        logger.error("Fiche du site '%s' non enregistrée après déploiement : %s", site_id, exc)
        return None


class AdminLifecycleAction(str, Enum):
    START = "start"
    STOP = "stop"
    FORCE_STOP = "force-stop"
    RESTART = "restart"
    REBUILD = "rebuild"


def rebuild_site(site_id: str) -> Any:
    """Recrée le conteneur depuis son dépôt (même identifiant, nouveau port, contenu resynchronisé).

    L'ancien conteneur est mis de côté (renommé, arrêté) puis supprimé seulement
    une fois le nouveau créé : en cas d'échec il est restauré tel quel.
    Sert aussi à (re)déployer une fiche dont le conteneur a disparu.
    """
    container_name = f"waas-{site_id}"
    record = db.get_site_record(site_id) or {}
    repo_url: Optional[str] = record.get("repo_url")
    try:
        existing = docker_client.containers.get(container_name)
        repo_url = repo_url or container_environment(existing).get("REPO_URL")
    except NotFound:
        existing = None
    if not repo_url:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Aucun dépôt connu pour ce site : redéployez-le avec une URL GitHub.")
    repo_url = validate_repo_url(repo_url)
    validate_container_api_url(api_public_url_setting())
    check_repo_access(repo_url)
    if existing is None:
        return provision_container(site_id, repo_url)

    was_running = existing.status == "running"
    existing.rename(f"{container_name}-rebuild-{int(time.time())}")
    if was_running:
        existing.stop(timeout=10)
    try:
        container = provision_container(site_id, repo_url)
    except Exception:
        restore_container(existing, container_name, was_running)
        raise
    try:
        existing.remove(force=True, v=True)
    except APIError as exc:
        logger.warning("Ancien conteneur de '%s' non supprimé après reconstruction : %s", site_id, exc)
    return container


def restore_container(previous: Any, container_name: str, was_running: bool) -> None:
    """Annule une reconstruction ratée : retire l'ébauche et rétablit l'ancien conteneur."""
    try:
        docker_client.containers.get(container_name).remove(force=True, v=True)
    except NotFound:
        pass
    except APIError as exc:
        logger.error("Conteneur partiel '%s' non supprimé : %s", container_name, exc)
    try:
        previous.rename(container_name)
        if was_running:
            previous.start()
    except APIError as exc:
        logger.error("Restauration de '%s' impossible : %s", container_name, exc)


@app.post("/admin/api/sites/{site_id}/action", dependencies=[Depends(require_admin)], tags=["Fleet Admin"])
def admin_manage_site_action(
    site_id: str,
    action: AdminLifecycleAction = Query(..., description="Action infrastructure sur le conteneur"),
    hard_resync: bool = Query(
        default=False,
        description="Si True avec 'restart', purge Supabase avant réinitialisation",
    ),
) -> dict[str, Any]:
    """Contrôle total sur l'état du conteneur (suspension, reprise, arrêt d'urgence, reconstruction)."""
    if not docker_client:
        raise HTTPException(status_code=503, detail="Daemon Docker inaccessible.")

    container_name = f"waas-{site_id}"
    action_timestamp = int(time.time())

    if action == AdminLifecycleAction.REBUILD:
        try:
            container = rebuild_site(site_id)
        except APIError as e:
            raise HTTPException(status_code=500, detail=f"Reconstruction impossible : {str(e)}")
        _invalidate_cache()
        host_port = get_container_web_port(container)
        return {
            "status": "success", "action": action.value, "site_id": site_id, "tenant_id": site_id,
            "action_timestamp": action_timestamp, "container_status": container.status,
            "host_port": host_port, "url": build_site_url(host_port),
        }

    try:
        container = docker_client.containers.get(container_name)
    except NotFound:
        raise HTTPException(status_code=404, detail=f"Conteneur '{container_name}' introuvable.")
    except APIError as e:
        raise HTTPException(status_code=500, detail=f"Erreur Docker : {str(e)}")

    try:
        if action == AdminLifecycleAction.START:
            container.start()
        elif action == AdminLifecycleAction.STOP:
            container.stop(timeout=10)
        elif action == AdminLifecycleAction.FORCE_STOP:
            container.kill()
        elif action == AdminLifecycleAction.RESTART:
            if hard_resync:
                db.delete_tenant_content(site_id)
            container.restart()

        container.reload()
        host_port = get_container_web_port(container)

        return {
            "status": "success",
            "action": action.value,
            "site_id": site_id,
            "tenant_id": site_id,
            "action_timestamp": action_timestamp,
            "container_status": container.status,
            "host_port": host_port,
            "url": build_site_url(host_port),
        }

    except APIError as e:
        raise HTTPException(status_code=500, detail=f"Erreur lors de l'exécution : {str(e)}")


@app.post("/waas/admin/tenant/{tenant_id}/action", dependencies=[Depends(require_admin)], tags=["Fleet Admin"], include_in_schema=False)
def admin_manage_tenant_action_legacy(
    tenant_id: str,
    action: AdminLifecycleAction = Query(...),
    hard_resync: bool = Query(default=False),
) -> dict[str, Any]:
    return admin_manage_site_action(site_id=tenant_id, action=action, hard_resync=hard_resync)


@app.get("/admin/api/sites/{site_id}/logs", dependencies=[Depends(require_admin)], tags=["Fleet Admin"])
def get_admin_site_logs(
    site_id: str,
    tail: int = Query(default=100, ge=1, le=2000, description="Nombre de lignes récentes à extraire"),
    timestamps: bool = Query(default=True, description="Inclure les horodatages Docker"),
) -> dict[str, Any]:
    """Extrait un instantané JSON des N dernières lignes de logs d'un conteneur."""
    if not docker_client:
        raise HTTPException(status_code=503, detail="Daemon Docker inaccessible.")

    container_name = f"waas-{site_id}"
    try:
        container = docker_client.containers.get(container_name)
    except NotFound:
        raise HTTPException(status_code=404, detail=f"Conteneur '{container_name}' introuvable.")
    except APIError as e:
        raise HTTPException(status_code=500, detail=f"Erreur Docker : {str(e)}")

    try:
        raw_logs = container.logs(
            stdout=True,
            stderr=True,
            tail=tail,
            timestamps=timestamps,
        )
        decoded = raw_logs.decode("utf-8", errors="replace")
        lines = [line for line in decoded.splitlines() if line.strip()]

        return {
            "site_id": site_id,
            "tenant_id": site_id,
            "container_id": container.short_id,
            "container_status": container.status,
            "count": len(lines),
            "logs": lines,
        }
    except APIError as e:
        raise HTTPException(status_code=500, detail=f"Erreur lecture logs : {str(e)}")


@app.get("/waas/admin/tenant/{tenant_id}/logs", dependencies=[Depends(require_admin)], tags=["Fleet Admin"], include_in_schema=False)
def get_admin_tenant_logs_legacy(tenant_id: str, tail: int = 100, timestamps: bool = True) -> dict[str, Any]:
    return get_admin_site_logs(site_id=tenant_id, tail=tail, timestamps=timestamps)


@app.get("/admin/api/sites/{site_id}/logs/stream", dependencies=[Depends(require_admin)], tags=["Fleet Admin"])
def admin_stream_site_logs(
    site_id: str,
    since: int | None = Query(default=None, description="Timestamp UNIX de départ (optionnel)"),
):
    """Lecture temps réel des logs en flux continu SSE pour n'importe quel conteneur."""
    if not docker_client:
        raise HTTPException(status_code=503, detail="Daemon Docker inaccessible.")

    container_name = f"waas-{site_id}"
    try:
        container = docker_client.containers.get(container_name)
    except NotFound:
        raise HTTPException(status_code=404, detail=f"Conteneur '{container_name}' introuvable.")

    def log_generator():
        valid_since = since if (since is not None and since > 0) else None

        log_stream = container.logs(
            stream=True,
            follow=True,
            since=valid_since,
            stdout=True,
            stderr=True,
        )
        try:
            for chunk in log_stream:
                line = chunk.decode("utf-8", errors="replace").rstrip()
                if line:
                    yield f"data: {line}\n\n"
                if "Démarrage de Nginx..." in line or "ready for start up" in line:
                    yield "data: [DONE] Initialisation terminée.\n\n"
                    break
        except GeneratorExit:
            log_stream.close()
        except Exception:
            yield "data: [Stream interrompu]\n\n"

    return StreamingResponse(
        log_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/waas/admin/tenant/{tenant_id}/logs/stream", dependencies=[Depends(require_admin)], tags=["Fleet Admin"], include_in_schema=False)
def admin_stream_tenant_logs_legacy(tenant_id: str, since: int | None = None):
    return admin_stream_site_logs(site_id=tenant_id, since=since)


@app.get("/admin/api/sites/{site_id}/analytics", dependencies=[Depends(require_admin)], tags=["Fleet Admin"])
def admin_get_site_analytics(site_id: str) -> dict[str, Any]:
    """Statistiques de trafic d'un site spécifique pour l'administrateur."""
    stats = db.get_tenant_analytics(site_id)
    return {
        "site_id": site_id,
        "tenant_id": site_id,
        "unique_visitors": stats["unique_visitors"],
        "total_page_views": stats["total_page_views"],
    }


@app.get("/waas/admin/tenant/{tenant_id}/analytics", dependencies=[Depends(require_admin)], tags=["Fleet Admin"], include_in_schema=False)
def admin_get_tenant_analytics_legacy(tenant_id: str) -> dict[str, Any]:
    return admin_get_site_analytics(site_id=tenant_id)


@app.patch("/admin/api/sites/{site_id}/content", tags=["Fleet Admin"])
def admin_update_site_content(
    site_id: str,
    payload: UpdateContentRequest,
    admin: Annotated[dict[str, Any], Depends(require_admin)],
    auto_publish: bool = Query(default=False),
) -> dict[str, Any]:
    """Permet à l'administrateur de modifier directement le contenu d'un site."""
    updated_record = _store_single_change(site_id, payload, admin.get("username"))
    response_data: dict[str, Any] = {
        "status": "success",
        "site_id": site_id,
        "tenant_id": site_id,
        "updated": updated_record,
        "published": False,
    }
    if auto_publish:
        response_data.update(_publish_fields(publish_site_content(site_id)))
    return response_data


@app.patch("/waas/admin/tenant/{tenant_id}/content", tags=["Fleet Admin"], include_in_schema=False)
def admin_update_tenant_content_legacy(
    tenant_id: str,
    payload: UpdateContentRequest,
    admin: Annotated[dict[str, Any], Depends(require_admin)],
    auto_publish: bool = False,
) -> dict[str, Any]:
    return admin_update_site_content(site_id=tenant_id, payload=payload, admin=admin, auto_publish=auto_publish)


@app.get("/admin/api/fleet/stats", dependencies=[Depends(require_admin)], tags=["Fleet Admin"])
@app.get("/waas/fleet/stats", dependencies=[Depends(require_admin)], tags=["Fleet Admin"], include_in_schema=False)
def get_fleet_stats() -> dict[str, Any]:
    """Synthèse des ressources consommées servie en O(1) mémoire vive par le moteur de télémétrie."""
    return telemetry_engine.get_fleet_stats()


@app.get("/admin/api/fleet/telemetry", dependencies=[Depends(require_admin)], tags=["Fleet Admin"])
def get_fleet_unified_telemetry() -> dict[str, Any]:
    """Ensemble unifié des métriques (stats + santé + kpis + métadonnées) servi en O(1) mémoire."""
    return telemetry_engine.get_unified_telemetry()


@app.get("/admin/api/fleet/kpis", dependencies=[Depends(require_admin)], tags=["Fleet Admin"])
def get_fleet_kpis() -> dict[str, Any]:
    """KPIs métier (clients, vitrines, MRR/ARR) servis en O(1) mémoire vive."""
    return telemetry_engine.get_business_kpis()


@app.delete("/admin/api/sites/{site_id}", dependencies=[Depends(require_admin)], tags=["Fleet Admin"])
def destroy_site(
    site_id: str,
    purge_record: bool = Query(default=False, description="Supprime aussi la fiche, les sondes et le trafic"),
) -> dict[str, Any]:
    """Suppression définitive du conteneur et purge des enregistrements Supabase."""
    if not docker_client:
        raise HTTPException(status_code=503, detail="Daemon Docker inaccessible.")

    container_name = f"waas-{site_id}"
    container_removed = False

    try:
        container = docker_client.containers.get(container_name)
        # v=True : supprime aussi le volume anonyme du contenu (sinon fuite disque).
        container.remove(force=True, v=True)
        container_removed = True
    except NotFound:
        container_removed = False
    except APIError as e:
        raise HTTPException(status_code=500, detail=f"Erreur Docker : {str(e)}")

    try:
        deleted_records_count = db.delete_tenant_content(site_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur Supabase : {str(e)}")

    record_deleted = False
    if purge_record:
        # Factures et tickets sont conservés (historique comptable et support).
        record_deleted = db.delete_site_record(site_id)
        monitors_deleted = db.delete_tenant_monitors(site_id)
        hits_deleted = db.purge_tenant_analytics(site_id)
        uptime_deleted = db.delete_uptime_rows(f"site:{site_id}")
        telemetry_engine.forget_site(site_id)
        # Plus aucun ancien rattachement : un futur site du même nom repart sans propriétaire.
        try:
            db.clear_tenant_refs(site_id)
        except Exception as exc:
            logger.error("Rattachements tenant_id de '%s' non effacés : %s", site_id, exc)
        logger.info("Site %s purgé (fiche=%s, sondes=%d, hits=%d, jours de disponibilité=%d)",
                    site_id, record_deleted, monitors_deleted, hits_deleted, uptime_deleted)
    _invalidate_cache()

    return {
        "status": "success",
        "site_id": site_id,
        "tenant_id": site_id,
        "container_removed": container_removed,
        "database_records_deleted": deleted_records_count,
        "record_deleted": record_deleted,
    }


@app.delete("/waas/admin/tenant/{tenant_id}", dependencies=[Depends(require_admin)], tags=["Fleet Admin"], include_in_schema=False)
def destroy_tenant_legacy(tenant_id: str) -> dict[str, Any]:
    # Appel direct : chaque paramètre explicite (un défaut Query(...) serait « vrai »).
    return destroy_site(site_id=tenant_id, purge_record=False)


# =========================================================
# 4. AUDIT & PROBING GLOBAL ADMIN
# =========================================================

@app.get("/admin/api/fleet/health", dependencies=[Depends(require_admin)], tags=["Fleet Admin"])
@app.get("/waas/admin/health", dependencies=[Depends(require_admin)], tags=["Fleet Admin"], include_in_schema=False)
def admin_comprehensive_health_probe() -> dict[str, Any]:
    """Audit exhaustif de santé de flotte et d'infrastructure servi en O(1) mémoire vive par le moteur de télémétrie."""
    return telemetry_engine.get_fleet_health()


# =========================================================
# 4.1 COCKPIT — ÉCRANS D'ADMINISTRATION (port de l'ancien dashboard)
# =========================================================
# Chaque page de Front/admin appelle un endpoint composite (1 aller-retour) en
# plus de la coque commune (/admin/api/shell). Les états Docker sont toujours lus
# en direct ; les mesures viennent du dernier cycle de télémétrie (O(1)).

def environment_info() -> dict[str, Any]:
    base_domain = clean_domain(db.get_setting("client_base_domain", ""))
    production = is_production_mode() and bool(base_domain and base_domain not in ("localhost", "127.0.0.1"))
    landing_slug = (db.get_setting("landing_site_slug", "") or "").strip().lower()
    return {
        "mode": "production" if production else "local",
        "label": "Production" if production else "Local",
        "base_domain": base_domain if production else "localhost",
        "api_public_url": api_public_url_setting(),
        "browser_api_url": browser_api_url_setting(),
        "client_base_domain": db.get_setting("client_base_domain", "") or "",
        "landing_site_slug": landing_slug,
        "urls": {
            "dashboard": f"https://app.{base_domain}" if production else "http://localhost:8000/admin/dashboard",
            "api": f"https://api.{base_domain}" if production else "http://localhost:8000",
            "showcase": f"https://{base_domain}" if production else "http://localhost:8000",
            "wildcard": f"https://*.{base_domain}" if production else "http://localhost:<port>",
        },
    }


def site_names() -> dict[str, str]:
    return {r["site_id"]: r.get("name") or r["site_id"] for r in site_records()}


def business_overview(catalog: list[dict[str, Any]]) -> dict[str, Any]:
    """MRR depuis les fiches `sites` ; repli sur l'estimation historique de la télémétrie."""
    if any(entry["has_record"] for entry in catalog):
        return {**cockpit.business_kpis(catalog), "source": "sites"}
    legacy = telemetry_engine.get_business_kpis()
    sites = max(1, int(legacy.get("total_sites") or 0))
    return {
        "mrr": legacy.get("mrr", 0.0), "arr": legacy.get("arr", 0.0),
        "arpu": round(float(legacy.get("mrr") or 0.0) / sites, 2),
        "active_contracts": legacy.get("total_sites", 0), "paying_sites": legacy.get("total_sites", 0),
        "plan_counts": {}, "source": "estimation",
    }


@app.get("/admin/api/shell", tags=["Cockpit"])
def admin_shell(current_user: Annotated[dict[str, Any], Depends(require_admin)]) -> dict[str, Any]:
    """Coque commune des pages (sidebar, topbar) : profil, environnement, alertes, compteurs."""
    incidents = current_incidents()
    return {
        "user": {"username": current_user.get("username"), "role": current_user.get("role")},
        "environment": environment_info(),
        "live": telemetry_engine.traffic.live(),
        "open_tickets": db.count_open_tickets(),
        "alerts": cockpit.alerts_summary(incidents),
        "incidents": incidents[:6],
        "telemetry": {
            "last_collected_at": telemetry_engine.last_collected_at,
            "docker_available": docker_client is not None,
        },
    }


@app.get("/admin/api/overview", dependencies=[Depends(require_admin)], tags=["Cockpit"])
def admin_overview() -> dict[str, Any]:
    """Tableau de bord : KPIs, fréquentation 14 j, sites et derniers échanges."""
    catalog = load_catalog()
    traffic = telemetry_engine.get_traffic()
    names = site_names()
    tickets = [cockpit.shape_ticket(t, names) for t in db.list_tickets()]
    stats = telemetry_engine.get_fleet_stats()
    return {
        "kpis": {
            **business_overview(catalog),
            **cockpit.fleet_counts(catalog),
            "live": traffic["live"],
            "visitors_14d": traffic["visitors"],
            "pageviews_14d": traffic["pageviews"],
            "fleet_ram_mb": stats.get("total_memory_usage_mb", 0.0),
        },
        "traffic": {"series": traffic["series"], "loaded": traffic["loaded"]},
        "sites": catalog[:6],
        "sites_total": len(catalog),
        "tickets": tickets[:4],
        "open_tickets": sum(1 for t in tickets if t["status"] != "closed"),
        "docker_available": docker_client is not None,
    }


# ---------------------------------------------------------
# 4.2 FICHE SITE : MÉTADONNÉES, DISPONIBILITÉ, SONDES, CONSOLE
# ---------------------------------------------------------

class SiteUpdateRequest(CockpitModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    client_id: Optional[str] = Field(default=None, max_length=64)
    plan_id: Optional[str] = Field(default=None, pattern=r"^[a-z0-9_-]{1,32}$")
    price_monthly: Optional[float] = Field(default=None, ge=0, le=100000)
    domain: Optional[str] = Field(default=None, max_length=253)
    domain_registrar: Optional[str] = Field(default=None, max_length=120)
    domain_expires_at: Optional[date] = None
    ssl_expires_at: Optional[date] = None
    contract_since: Optional[date] = None
    contract_status: Optional[Literal["active", "paused", "cancelled"]] = None
    repo_url: Optional[str] = Field(default=None, max_length=500)
    notes: Optional[str] = Field(default=None, max_length=4000)


def require_site_entry(site_id: str) -> dict[str, Any]:
    entry = find_site_entry(site_id)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site introuvable.")
    return entry


@app.get("/admin/api/sites/{site_id}", dependencies=[Depends(require_admin)], tags=["Cockpit"])
def admin_site_detail(site_id: str) -> dict[str, Any]:
    """Fiche complète : site, fréquentation, disponibilité 90 j, sondes, factures, échanges."""
    entry = require_site_entry(site_id)
    invoices = cockpit.flag_overdue(db.list_invoices(site_id=site_id), cockpit.today_utc())
    names = site_names()
    return {
        "site": entry,
        "record": db.get_site_record(site_id),
        "traffic": telemetry_engine.get_traffic(site_id),
        "availability": telemetry_engine.get_site_availability(site_id),
        "monitors": db.list_tenant_monitors(site_id),
        "content_count": db.count_tenant_content(site_id),
        "invoices": invoices,
        "invoice_totals": cockpit.invoice_totals(invoices),
        "tickets": [cockpit.shape_ticket(t, names) for t in db.list_tickets(site_id=site_id)],
        "clients": [{"id": c.get("id"), "username": c.get("username")} for c in all_clients()],
        "plans": list(cockpit.PLANS),
        "private_repos": git_source.private_repos_enabled(),
        "environment": environment_info(),
        "docker_available": docker_client is not None,
    }


@app.patch("/admin/api/sites/{site_id}", dependencies=[Depends(require_admin)], tags=["Cockpit"])
def admin_update_site(site_id: str, payload: SiteUpdateRequest) -> dict[str, Any]:
    """Met à jour la fiche commerciale (formule, client, domaine, échéances, notes)."""
    entry = require_site_entry(site_id)
    fields: dict[str, Any] = payload.model_dump(exclude_unset=True)
    # Colonnes obligatoires : une valeur nulle signifie « inchangé », jamais un effacement.
    for key in ("name", "contract_since", "contract_status", "plan_id", "price_monthly", "notes"):
        if key in fields and fields[key] is None:
            fields.pop(key)
    if "plan_id" in fields or "price_monthly" in fields:
        fields.update(plan_fields(fields.pop("plan_id", None), fields.pop("price_monthly", None)))
    if "domain" in fields:
        fields["domain"] = normalize_domain(fields["domain"])
    if "repo_url" in fields and fields["repo_url"]:
        fields["repo_url"] = validate_repo_url(fields["repo_url"])
        # Le formulaire renvoie toujours le dépôt : GitHub n'est consulté que s'il change.
        if fields["repo_url"] != entry.get("repo_url"):
            check_repo_access(fields["repo_url"])
    if "client_id" in fields:
        client_id = (fields["client_id"] or "").strip() or None
        if client_id:
            _require_client(client_id)
        fields["client_id"] = client_id
    for key in ("domain_expires_at", "ssl_expires_at", "contract_since"):
        if isinstance(fields.get(key), date):
            fields[key] = fields[key].isoformat()
    if isinstance(fields.get("name"), str):
        fields["name"] = fields["name"].strip()
    record = db.upsert_site_record(site_id, fields)
    if "client_id" in fields:
        try:  # la fiche fait foi ; on retire les anciens liens tenant_id des autres comptes
            db.clear_tenant_refs(site_id, keep_user_id=fields["client_id"])
        except Exception as exc:
            logger.warning("Rattachements tenant_id de '%s' non nettoyés : %s", site_id, exc)
    _invalidate_cache()
    return {"status": "success", "record": record, "site": find_site_entry(site_id)}


class ExecRequest(CockpitModel):
    command: str = Field(..., min_length=1, max_length=500)


EXEC_TIMEOUT_SECONDS = 10
EXEC_KILL_AFTER_SECONDS = 2
EXEC_OUTPUT_LIMIT = 64 * 1024
EXEC_MAX_CONCURRENT = 4
#: La commande est passée en argument ($0), jamais interpolée : aucun problème de guillemets.
#: ``-k`` : SIGKILL si la commande ignore SIGTERM (sinon l'exec survivrait au délai).
_EXEC_WRAPPER = (
    f'if timeout -k 1 1 true >/dev/null 2>&1; then exec timeout -k {EXEC_KILL_AFTER_SECONDS} '
    f'{EXEC_TIMEOUT_SECONDS} sh -c "$0"; '
    f'elif command -v timeout >/dev/null 2>&1; then exec timeout {EXEC_TIMEOUT_SECONDS} sh -c "$0"; '
    'else exec sh -c "$0"; fi'
)
#: Pool dédié et borné : une console bloquée n'accapare jamais le pool partagé des routes
#: synchrones ; au-delà de EXEC_MAX_CONCURRENT commandes en cours -> 429.
_EXEC_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=EXEC_MAX_CONCURRENT, thread_name_prefix="waas-exec")
_EXEC_SLOTS = threading.BoundedSemaphore(EXEC_MAX_CONCURRENT)


@app.post("/admin/api/sites/{site_id}/exec", tags=["Cockpit"])
async def admin_exec_in_container(
    site_id: str,
    payload: ExecRequest,
    current_user: Annotated[dict[str, Any], Depends(require_admin)],
) -> dict[str, Any]:
    """Console : exécute une commande dans le conteneur (utilisateur non privilégié, 10 s max)."""
    if not docker_client:
        raise HTTPException(status_code=503, detail="Daemon Docker inaccessible.")
    command = payload.command.strip()
    if "\x00" in command:
        raise HTTPException(status_code=422, detail="Commande invalide.")

    def run() -> tuple[int, bytes, float]:
        try:
            container = docker_client.containers.get(f"waas-{site_id}")
            if container.status != "running":
                raise HTTPException(status_code=409, detail="Conteneur arrêté : démarrez-le pour utiliser la console.")
            started = time.monotonic()
            result = container.exec_run(
                ["sh", "-c", _EXEC_WRAPPER, command],
                user="10001:10001", workdir=TENANT_CONTENT_PATH, stdout=True, stderr=True, tty=False,
            )
            return int(result.exit_code or 0), result.output or b"", time.monotonic() - started
        finally:
            # Libéré par le thread lui-même : un délai dépassé côté API ne libère pas
            # une place tant que l'exec tourne encore.
            _EXEC_SLOTS.release()

    if not _EXEC_SLOTS.acquire(blocking=False):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="Trop de commandes en cours : réessayez dans quelques secondes.")
    loop = asyncio.get_running_loop()
    try:
        future = loop.run_in_executor(_EXEC_POOL, run)
    except RuntimeError:
        _EXEC_SLOTS.release()
        raise
    try:
        exit_code, output, elapsed = await asyncio.wait_for(
            future, timeout=EXEC_TIMEOUT_SECONDS + EXEC_KILL_AFTER_SECONDS + 6,
        )
    except NotFound:
        raise HTTPException(status_code=404, detail=f"Conteneur 'waas-{site_id}' introuvable.")
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Commande trop longue : interrompue côté API.")
    except APIError as exc:
        raise HTTPException(status_code=500, detail=f"Erreur Docker : {exc}")

    logger.info("Console admin '%s' sur %s : %r (code %s)", current_user.get("username"), site_id, command, exit_code)
    return {
        "site_id": site_id,
        "command": command,
        "exit_code": exit_code,
        "timed_out": exit_code in (124, 143),
        "output": output[:EXEC_OUTPUT_LIMIT].decode("utf-8", errors="replace"),
        "truncated": len(output) > EXEC_OUTPUT_LIMIT,
        "duration_ms": round(elapsed * 1000, 1),
    }


@app.post("/admin/api/sites/{site_id}/probe", dependencies=[Depends(require_admin)], tags=["Cockpit"])
async def admin_probe_site(site_id: str) -> dict[str, Any]:
    """Contrôle immédiat : page d'accueil de la vitrine et sondes externes du site."""
    result = await telemetry_engine.probe_site_now(site_id)
    return {"result": result, "availability": telemetry_engine.get_site_availability(site_id)}


@app.get("/admin/api/sites/{site_id}/availability", dependencies=[Depends(require_admin)], tags=["Cockpit"])
def admin_site_availability(site_id: str) -> dict[str, Any]:
    return telemetry_engine.get_site_availability(site_id)


class MonitorCreateRequest(CockpitModel):
    name: str = Field(..., min_length=1, max_length=80)
    monitor_type: Literal["http", "tcp"] = "http"
    target: str = Field(..., min_length=3, max_length=500)
    method: Literal["GET", "HEAD"] = "GET"
    expected_status: int = Field(default=200, ge=100, le=599)
    timeout_seconds: float = Field(default=5.0, ge=0.5, le=30)


@app.get("/admin/api/sites/{site_id}/monitors", dependencies=[Depends(require_admin)], tags=["Cockpit"])
def admin_list_monitors(site_id: str) -> dict[str, Any]:
    monitors = db.list_tenant_monitors(site_id)
    return {"count": len(monitors), "monitors": monitors}


@app.post("/admin/api/sites/{site_id}/monitors", dependencies=[Depends(require_admin)], tags=["Cockpit"],
          status_code=201)
async def admin_create_monitor(site_id: str, payload: MonitorCreateRequest) -> dict[str, Any]:
    """Ajoute une sonde externe (API tierce, SMTP, réservation…) après contrôle anti-SSRF."""
    is_url = payload.monitor_type == "http"
    safe, reason, _ip, _port = await is_safe_external_target(payload.target.strip(), is_url=is_url)
    if not safe:
        raise HTTPException(status_code=422, detail=f"Cible refusée : {reason}")
    created = await asyncio.to_thread(db.create_tenant_monitor, {
        "tenant_id": site_id,
        "name": payload.name.strip(),
        "monitor_type": payload.monitor_type,
        "target": payload.target.strip(),
        "method": payload.method,
        "expected_status": payload.expected_status,
        "timeout_seconds": payload.timeout_seconds,
        "is_active": True,
    })
    if not created:
        raise HTTPException(status_code=502, detail="Enregistrement de la sonde impossible.")
    return {"status": "success", "monitor": created}


@app.delete("/admin/api/sites/{site_id}/monitors/{monitor_id}", dependencies=[Depends(require_admin)],
            tags=["Cockpit"])
def admin_delete_monitor(site_id: str, monitor_id: str) -> dict[str, Any]:
    if not db.delete_tenant_monitor(monitor_id, tenant_id=site_id):
        raise HTTPException(status_code=404, detail="Sonde introuvable.")
    return {"status": "success", "deleted": monitor_id}


# ---------------------------------------------------------
# 4.3 FACTURATION & RENOUVELLEMENTS
# ---------------------------------------------------------

class InvoiceCreateRequest(CockpitModel):
    site_id: str = Field(..., pattern=r"^[a-z0-9-]+$")
    amount: float = Field(..., ge=0, le=1_000_000)
    description: str = Field(default="Abonnement vitrine & hébergement", min_length=1, max_length=200)
    status: Literal["draft", "pending", "paid"] = "pending"
    issued_at: Optional[date] = None
    due_at: Optional[date] = None


class InvoiceStatusRequest(BaseModel):
    status: Literal["draft", "pending", "paid", "overdue"]


INVOICE_DUE_DAYS = 14
DEFAULT_AGENCY_NAME = "29th studio"


def invoice_payload(entry: dict[str, Any], amount: float, description: str, status_value: str,
                    issued: date, due: Optional[date], period: Optional[str] = None) -> dict[str, Any]:
    item: dict[str, Any] = {"description": description, "quantity": 1, "unit_price": round(amount, 2),
                            "total": round(amount, 2)}
    if period:  # marqueur d'idempotence de la facturation mensuelle
        item.update(kind="subscription", period=period)
    return {
        "site_id": entry["site_id"],
        "client_id": entry.get("client_id"),
        "client_name": entry.get("client_name") or "",
        "description": description,
        "amount": round(amount, 2),
        "status": status_value,
        "issued_at": issued.isoformat(),
        "due_at": (due or issued + timedelta(days=INVOICE_DUE_DAYS)).isoformat(),
        "paid_at": datetime.now(timezone.utc).isoformat() if status_value == "paid" else None,
        "items": [item],
    }


def shaped_invoices(site_id: Optional[str] = None) -> list[dict[str, Any]]:
    names = site_names()
    invoices = cockpit.flag_overdue(db.list_invoices(site_id=site_id), cockpit.today_utc())
    return [{**inv, "site_name": names.get(str(inv.get("site_id") or ""), inv.get("site_id") or "—")}
            for inv in invoices]


@app.get("/admin/api/invoices", dependencies=[Depends(require_admin)], tags=["Cockpit"])
def admin_list_invoices() -> dict[str, Any]:
    invoices = shaped_invoices()
    catalog = load_catalog(inventory=telemetry_engine.get_inventory())
    return {
        "invoices": invoices,
        "totals": cockpit.invoice_totals(invoices),
        "business": business_overview(catalog),
        "sites": [{"site_id": s["site_id"], "name": s["name"], "price_monthly": s["price_monthly"],
                   "client_name": s["client_name"]} for s in catalog],
        "schema_ready": db.relation_ready(db.TABLE_INVOICES),
    }


@app.post("/admin/api/invoices", dependencies=[Depends(require_admin)], tags=["Cockpit"], status_code=201)
def admin_create_invoice(payload: InvoiceCreateRequest) -> dict[str, Any]:
    entry = require_site_entry(payload.site_id)
    issued = payload.issued_at or cockpit.today_utc()
    invoice = db.create_invoice(invoice_payload(entry, payload.amount, payload.description.strip(),
                                                payload.status, issued, payload.due_at))
    return {"status": "success", "invoice": invoice}


#: Une seule génération à la fois (double clic, deux onglets) : sinon deux passes
#: concurrentes liraient le même état et factureraient deux fois.
_INVOICE_GENERATION_LOCK = threading.Lock()


@app.post("/admin/api/invoices/generate", dependencies=[Depends(require_admin)], tags=["Cockpit"])
def admin_generate_monthly_invoices() -> dict[str, Any]:
    """Émet la facture du mois de chaque contrat actif payant (jamais deux fois le même mois)."""
    if not _INVOICE_GENERATION_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Génération déjà en cours.")
    try:
        today = cockpit.today_utc()
        period = today.strftime("%Y-%m")
        already = db.subscription_invoiced_sites(period, since_day=today.replace(day=1).isoformat())
        created = []
        for entry in load_catalog(inventory=telemetry_engine.get_inventory()):
            if not entry["has_record"] or entry["contract_status"] != "active" or entry["price_monthly"] <= 0:
                continue
            if entry["site_id"] in already:
                continue
            label = f"{entry['plan_name']} · {today.strftime('%m/%Y')} — {entry['name']}"
            created.append(db.create_invoice(invoice_payload(entry, entry["price_monthly"], label, "pending",
                                                             today, None, period=period)))
        return {"status": "success", "created": len(created), "invoices": created}
    finally:
        _INVOICE_GENERATION_LOCK.release()


@app.get("/admin/api/invoices/{invoice_id}", dependencies=[Depends(require_admin)], tags=["Cockpit"])
def admin_get_invoice(invoice_id: str) -> dict[str, Any]:
    invoice = db.get_invoice(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Facture introuvable.")
    return receipt_payload(invoice)


@app.post("/admin/api/invoices/{invoice_id}/pay", dependencies=[Depends(require_admin)], tags=["Cockpit"])
def admin_mark_invoice_paid(invoice_id: str) -> dict[str, Any]:
    updated = db.update_invoice(invoice_id, {"status": "paid", "paid_at": datetime.now(timezone.utc).isoformat()})
    if not updated:
        raise HTTPException(status_code=404, detail="Facture introuvable.")
    return {"status": "success", "invoice": updated}


@app.post("/admin/api/invoices/{invoice_id}/status", dependencies=[Depends(require_admin)], tags=["Cockpit"])
def admin_set_invoice_status(invoice_id: str, payload: InvoiceStatusRequest) -> dict[str, Any]:
    fields: dict[str, Any] = {"status": payload.status}
    fields["paid_at"] = datetime.now(timezone.utc).isoformat() if payload.status == "paid" else None
    updated = db.update_invoice(invoice_id, fields)
    if not updated:
        raise HTTPException(status_code=404, detail="Facture introuvable.")
    return {"status": "success", "invoice": updated}


@app.delete("/admin/api/invoices/{invoice_id}", dependencies=[Depends(require_admin)], tags=["Cockpit"])
def admin_delete_invoice(invoice_id: str) -> dict[str, Any]:
    if not db.delete_invoice(invoice_id):
        raise HTTPException(status_code=404, detail="Facture introuvable.")
    return {"status": "success", "deleted": invoice_id}


def receipt_payload(invoice: dict[str, Any]) -> dict[str, Any]:
    """Facture + émetteur (réglages agency_*) + vitrine : tout ce qu'il faut pour le reçu."""
    site_id = str(invoice.get("site_id") or "")
    record = db.get_site_record(site_id) if site_id else None
    shaped = cockpit.flag_overdue([invoice], cockpit.today_utc())[0]
    return {
        "invoice": shaped,
        "site": {"site_id": site_id, "name": (record or {}).get("name") or site_id,
                 "domain": (record or {}).get("domain") or ""},
        "issuer": {
            "name": db.get_setting("agency_name", DEFAULT_AGENCY_NAME) or DEFAULT_AGENCY_NAME,
            "email": db.get_setting("agency_email", "") or "",
            "address": db.get_setting("agency_address", "Lausanne, Suisse") or "",
            "vat": db.get_setting("agency_vat", "") or "",
        },
    }


@app.get("/admin/api/renewals", dependencies=[Depends(require_admin)], tags=["Cockpit"])
def admin_renewals() -> dict[str, Any]:
    """Échéancier : abonnements, domaines, certificats SSL et factures impayées."""
    catalog = load_catalog(inventory=telemetry_engine.get_inventory())
    invoices = cockpit.flag_overdue(db.list_invoices(), cockpit.today_utc())
    renewals = cockpit.collect_renewals(catalog, invoices, cockpit.today_utc(),
                                        db.get_setting("client_base_domain", "") or "")
    return {"renewals": renewals, "kpis": cockpit.renewal_kpis(renewals),
            "schema_ready": db.relation_ready(db.TABLE_SITES)}


# ---------------------------------------------------------
# 4.4 SUPPORT (MESSAGERIE AGENCE)
# ---------------------------------------------------------

class TicketReplyRequest(CockpitModel):
    body: str = Field(..., min_length=1, max_length=5000)


class TicketStatusRequest(BaseModel):
    status: Literal["open", "waiting", "closed"]


class TicketCreateRequest(CockpitModel):
    subject: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1, max_length=5000)
    priority: Literal["low", "normal", "urgent"] = "normal"


def require_ticket(ticket_id: str) -> dict[str, Any]:
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Conversation introuvable.")
    return ticket


@app.get("/admin/api/tickets", dependencies=[Depends(require_admin)], tags=["Cockpit"])
def admin_list_tickets(
    view: Literal["open", "all"] = Query(default="open", alias="filter",
                                         description="open : non résolus · all : tous"),
) -> dict[str, Any]:
    names = site_names()
    tickets = [cockpit.shape_ticket(t, names) for t in db.list_tickets()]
    open_tickets = [t for t in tickets if t["status"] != "closed"]
    return {
        "tickets": open_tickets if view == "open" else tickets,
        "counts": {"open": len(open_tickets), "all": len(tickets)},
        "filter": view,
        "schema_ready": db.relation_ready(db.TABLE_TICKETS),
    }


@app.get("/admin/api/tickets/{ticket_id}", dependencies=[Depends(require_admin)], tags=["Cockpit"])
def admin_get_ticket(ticket_id: str) -> dict[str, Any]:
    ticket = require_ticket(ticket_id)
    db.mark_ticket_seen(ticket_id, "agency")
    return {"ticket": cockpit.shape_ticket(ticket, site_names(), with_messages=True)}


def author_label(current_user: dict[str, Any]) -> str:
    """Signature d'un message : le nom de l'agence pour l'équipe (jamais l'identifiant de
    connexion d'un administrateur, visible du client), le nom du compte sinon."""
    if current_user.get("role") == "admin":
        return (str(db.get_setting("agency_name", "") or "").strip() or DEFAULT_AGENCY_NAME)[:80]
    return str(current_user.get("username") or "Client")[:80]


@app.post("/admin/api/tickets/{ticket_id}/messages", tags=["Cockpit"])
def admin_reply_ticket(
    ticket_id: str,
    payload: TicketReplyRequest,
    current_user: Annotated[dict[str, Any], Depends(require_admin)],
) -> dict[str, Any]:
    require_ticket(ticket_id)
    db.add_ticket_message(ticket_id, payload.body.strip(), author_label(current_user), "agency")
    return {"ticket": cockpit.shape_ticket(require_ticket(ticket_id), site_names(), with_messages=True)}


@app.post("/admin/api/tickets/{ticket_id}/status", dependencies=[Depends(require_admin)], tags=["Cockpit"])
def admin_set_ticket_status(ticket_id: str, payload: TicketStatusRequest) -> dict[str, Any]:
    require_ticket(ticket_id)
    db.set_ticket_status(ticket_id, payload.status)
    return {"ticket": cockpit.shape_ticket(require_ticket(ticket_id), site_names(), with_messages=True)}


# ---------------------------------------------------------
# 4.5 SANTÉ DE L'INFRASTRUCTURE
# ---------------------------------------------------------

_DOCKER_VERSION: dict[str, Any] = {"value": None, "at": 0.0}


def docker_version() -> Optional[str]:
    if not docker_client:
        return None
    if time.monotonic() - _DOCKER_VERSION["at"] > 600 or not _DOCKER_VERSION["value"]:
        try:
            _DOCKER_VERSION["value"] = (docker_client.version() or {}).get("Version")
        except (APIError, DockerException) as exc:
            logger.debug("Version Docker illisible : %s", exc)
            _DOCKER_VERSION["value"] = None
        _DOCKER_VERSION["at"] = time.monotonic()
    return _DOCKER_VERSION["value"]


def health_subsystems(health: dict[str, Any]) -> list[dict[str, Any]]:
    infra = health.get("infrastructure") or {}
    database = infra.get("database") or {}
    docker_check = infra.get("docker_engine") or {}
    diag = telemetry_engine.get_diagnostics()
    dns = diag.get("dns") or {}
    uptime_ready = db.relation_ready(db.TABLE_UPTIME)
    supabase_host = urlparse(db.SUPABASE_URL or "").hostname or "—"

    def state(value: Any) -> str:
        return {"UP": "up", "DOWN": "down"}.get(str(value or "").upper(), "unknown")

    return [
        {"name": "API WaaS", "role": "Plan de contrôle (FastAPI)", "endpoint": api_public_url_setting() or "local",
         "state": "up", "detail": "Documentation /docs active" if DOCS_ENABLED else "Documentation désactivée"},
        {"name": "Supabase", "role": "Base de données, stockage, comptes", "endpoint": supabase_host,
         "state": state(database.get("status")),
         "detail": f"{database.get('latency_ms')} ms" if database.get("latency_ms") is not None else "—"},
        {"name": "Moteur Docker", "role": "Conteneurs des vitrines", "endpoint": docker_version() or "—",
         "state": state(docker_check.get("status")) if docker_client else "down",
         "detail": "Démon joignable" if docker_client else "Démon injoignable depuis l'API"},
        {"name": "Moteur de télémétrie", "role": "Collecte en arrière-plan", "endpoint": diag.get("policy", {}).get("source"),
         "state": "up" if diag.get("running") else "down",
         "detail": f"{diag.get('abandoned_blocking_threads', 0)} appel(s) bloquant(s) abandonné(s)"},
        {"name": "Résolveur DNS", "role": "Sondes externes (anti-SSRF)", "endpoint": f"{dns.get('threads', 0)} thread(s)",
         "state": "degraded" if dns.get("rejected") else "up",
         "detail": f"{dns.get('completed', 0)} résolutions · {dns.get('rejected', 0)} refus"},
        {"name": "Historique Supabase", "role": "SLA 90 j & charge horaire", "endpoint": db.TABLE_UPTIME,
         "state": "up" if uptime_ready else "degraded",
         "detail": "Persisté" if uptime_ready else "Mémoire seule : appliquez la migration"},
    ]


def health_overview_payload() -> dict[str, Any]:
    health = telemetry_engine.get_fleet_health()
    host = telemetry_engine.get_host()
    stats = telemetry_engine.get_fleet_stats()
    inventory = telemetry_engine.get_inventory()
    catalog = load_catalog(inventory=inventory)
    incidents = current_incidents(catalog)
    probes = telemetry_engine.get_probe_log(40)
    latencies = [p["latency_ms"] for p in probes if p.get("latency_ms") is not None]
    failing = [p for p in probes if p.get("state") not in ("up",)]
    fleet_mem_gb = round(float(stats.get("total_memory_usage_mb") or 0.0) / 1024, 2)
    used = host.get("ram_used_gb")
    cache = host.get("ram_cache_gb") or 0.0
    memory_split = None
    if used is not None:
        memory_split = {
            "containers_gb": fleet_mem_gb,
            "cache_gb": round(cache, 2),
            "system_gb": round(max(0.0, float(used) - fleet_mem_gb - cache), 2),
            "available_gb": host.get("ram_available_gb"),
        }
    top = sorted((i for i in inventory if i.get("memory_mb")), key=lambda i: -float(i["memory_mb"]))[:8]
    return {
        "status": health.get("status", "UNKNOWN"),
        "collected_at": health.get("cached_at"),
        "probe_duration_ms": health.get("probe_duration_ms"),
        "incidents": incidents,
        "host": host,
        "sla": telemetry_engine.get_sla(),
        "services": telemetry_engine.get_availability(),
        "subsystems": health_subsystems(health),
        "probes": probes,
        "fleet": {**(health.get("fleet") or {}), "containers": None},
        "external": {k: v for k, v in (health.get("external_services") or {}).items() if k != "results"},
        "kpis": {
            "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
            "error_rate": round(len(failing) / len(probes) * 100, 1) if probes else 0.0,
            "probes_total": len(probes),
            "probes_ok": len(probes) - len(failing),
        },
        "memory_split": memory_split,
        "top_containers": [{"site_id": i["site_id"], "memory_mb": i["memory_mb"],
                            "cpu_percent": i.get("cpu_percent")} for i in top],
        "fleet_ram_mb": stats.get("total_memory_usage_mb", 0.0),
    }


@app.get("/admin/api/health/overview", dependencies=[Depends(require_admin)], tags=["Cockpit"])
def admin_health_overview() -> dict[str, Any]:
    return health_overview_payload()


@app.post("/admin/api/health/probe", dependencies=[Depends(require_admin)], tags=["Cockpit"])
async def admin_health_probe_now() -> dict[str, Any]:
    """Passe complète immédiate (sondes, conteneurs, hôte, trafic), puis synthèse."""
    await telemetry_engine.collect_all()
    return await asyncio.to_thread(health_overview_payload)


@app.get("/admin/api/health/host", dependencies=[Depends(require_admin)], tags=["Cockpit"])
async def admin_health_host() -> dict[str, Any]:
    """Mesure fraîche de l'hôte (≤ 2 s) pour les jauges temps réel."""
    return await telemetry_engine.sample_host()


@app.get("/admin/api/health/timeline", dependencies=[Depends(require_admin)], tags=["Cockpit"])
async def admin_health_timeline(
    period: Literal["1h", "24h", "7d", "30d"] = Query(default="24h"),
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    if period in ("7d", "30d"):
        since = datetime.now(timezone.utc) - timedelta(days=7 if period == "7d" else 30)
        rows = await asyncio.to_thread(db.get_host_hourly, since.isoformat())
    return telemetry_engine.get_timeline(period, rows)


# ---------------------------------------------------------
# 4.6 PARAMÈTRES DE LA PLATEFORME
# ---------------------------------------------------------

KNOWN_SETTINGS: tuple[dict[str, Any], ...] = (
    {"key": "api_public_url", "group": "network", "label": "URL publique de l'API",
     "help": "Joignable depuis les conteneurs (jamais localhost). Pilote aussi le mode Production.",
     "placeholder": "https://api.exemple.com"},
    {"key": "browser_api_url", "group": "network", "label": "URL de l'API vue du navigateur",
     "help": "Studio, aperçu encadré et collecte d'audience. Peut être http://localhost:8000 en local. "
             "Vide : api_public_url. Un changement n'agit qu'après recréation des vitrines.",
     "placeholder": "https://api.exemple.com"},
    {"key": "client_base_domain", "group": "network", "label": "Domaine de base des vitrines",
     "help": "Hôte des URL de vitrines (http://<domaine>:<port>). Un domaine réel active le mode Production.",
     "placeholder": "localhost"},
    {"key": "landing_site_slug", "group": "network", "label": "Site vitrine officiel (domaine racine)",
     "help": "Identifiant du site (slug) servi sur le domaine racine (ex. 29th.site et www). Les autres sites sont sur {slug}.29th.site.",
     "placeholder": "dwadaw"},
    {"key": "tenant_port_binding", "group": "network", "label": "Interface d'écoute des vitrines",
     "help": "127.0.0.1 : derrière le reverse-proxy (recommandé). 0.0.0.0 : exposition directe.",
     "default": "127.0.0.1", "options": ["127.0.0.1", "0.0.0.0"]},
    {"key": "docker_base_image", "group": "fleet", "label": "Image Docker des vitrines",
     "help": "Image utilisée à chaque déploiement.", "default": "waas-base-image:latest"},
    {"key": "allowed_git_hosts", "group": "fleet", "label": "Hôtes Git autorisés",
     "help": "Liste blanche des dépôts de templates (anti-SSRF).", "default": "github.com,gitlab.com"},
    {"key": "agency_name", "group": "business", "label": "Nom de l'agence (factures)", "default": DEFAULT_AGENCY_NAME},
    {"key": "agency_email", "group": "business", "label": "E-mail de facturation", "placeholder": "facturation@…"},
    {"key": "agency_address", "group": "business", "label": "Adresse (factures)", "default": "Lausanne, Suisse"},
    {"key": "agency_vat", "group": "business", "label": "N° TVA / IDE", "placeholder": "CHE-…"},
    {"key": "monthly_price_per_site", "group": "business", "label": "Prix mensuel estimé (sans fiche)",
     "help": "Utilisé uniquement tant que la table `sites` n'est pas renseignée.", "default": "199"},
    {"key": "telemetry_probe_concurrency", "group": "telemetry", "label": "Sondes externes simultanées",
     "default": "10"},
    {"key": "telemetry_container_probe_concurrency", "group": "telemetry", "label": "Sondes locales simultanées",
     "default": "20"},
    {"key": "telemetry_allowed_http_methods", "group": "telemetry", "label": "Méthodes HTTP des sondes",
     "default": "GET,HEAD"},
    {"key": "telemetry_probe_port_allowlist", "group": "telemetry", "label": "Ports sondables (liste blanche)",
     "help": "Vide : tous sauf la liste noire.", "placeholder": "80,443,8080"},
    {"key": "telemetry_blocked_probe_ports", "group": "telemetry", "label": "Ports interdits supplémentaires"},
    {"key": "telemetry_max_monitors_per_collection", "group": "telemetry", "label": "Plafond de sondes par cycle",
     "help": "0 = illimité.", "default": "0"},
    {"key": "telemetry_block_self_targeting", "group": "telemetry", "label": "Interdire l'auto-sondage",
     "default": "1", "options": ["1", "0"]},
)


@app.get("/admin/api/platform", dependencies=[Depends(require_admin)], tags=["Cockpit"])
def admin_platform() -> dict[str, Any]:
    """Paramètres : réglages connus, environnement, schéma Supabase, sécurité, diagnostics."""
    db.reload_settings_cache()
    return {
        "settings": db.get_settings_snapshot(),
        "known": list(KNOWN_SETTINGS),
        "environment": environment_info(),
        "schema": db.probe_schema(),
        "migration_file": db.MIGRATION_FILE,
        "security": security_warnings(),
        "docs_enabled": DOCS_ENABLED,
        "private_repos": git_source.private_repos_enabled(),
        "docker": {"available": docker_client is not None, "version": docker_version()},
        "supabase_host": urlparse(db.SUPABASE_URL or "").hostname,
        "telemetry": telemetry_engine.get_diagnostics(),
        "host": {k: telemetry_engine.get_host().get(k) for k in ("hostname", "platform", "source")},
        "sites": [{"site_id": s["site_id"], "name": s.get("name") or s["site_id"]} for s in load_catalog()],
    }


# =========================================================
# 4.7 ESPACE CLIENT (PORTAIL) — DONNÉES FILTRÉES PAR PROPRIÉTÉ
# =========================================================

#: Champs d'un site exposés à un client (jamais l'image, le conteneur ni le dépôt).
CLIENT_SITE_FIELDS = (
    "site_id", "name", "domain", "url", "plan_id", "plan_name", "price_monthly", "contract_status",
    "contract_since", "status", "running", "live_visitors", "started_at_epoch", "client_name",
)


def accessible_sites(current_user: dict[str, Any]) -> list[dict[str, Any]]:
    """Sites visibles : tous pour l'agence, ceux du compte sinon (table `sites` ou tenant_id)."""
    catalog = load_catalog(inventory=telemetry_engine.get_inventory())
    if current_user.get("role") == "admin":
        return catalog
    # Règle de propriété unique (db.get_sites_by_client_id) : le `tenant_id` du jeton
    # n'est jamais ajouté tel quel, il peut être périmé.
    owned = {str(s.get("site_id") or s.get("id")) for s in db.get_sites_by_client_id(current_user["id"])}
    return [entry for entry in catalog if entry["site_id"] in owned]


def client_view(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: entry.get(key) for key in CLIENT_SITE_FIELDS}


def visible_invoices(site_id: str, current_user: dict[str, Any]) -> list[dict[str, Any]]:
    """Factures d'un site ; les brouillons ne sont visibles que de l'agence."""
    invoices = cockpit.flag_overdue(db.list_invoices(site_id=site_id), cockpit.today_utc())
    if current_user.get("role") == "admin":
        return invoices
    return [inv for inv in invoices if inv.get("status") != "draft"]


@app.get("/api/v1/portal/overview", tags=["Client Portal"])
def portal_overview(
    request: Request,
    current_user: Annotated[dict[str, Any], Depends(get_current_user)],
    site_id: Optional[str] = Query(default=None, pattern=r"^[a-z0-9-]+$"),
) -> dict[str, Any]:
    """Vue d'ensemble de l'espace client pour un site accessible (le premier par défaut)."""
    sites = accessible_sites(current_user)
    base = {
        "user": {"username": current_user.get("username"), "role": current_user.get("role")},
        "sites": [{"site_id": s["site_id"], "name": s["name"]} for s in sites],
    }
    if not sites:
        return {**base, "site": None}
    entry = next((s for s in sites if s["site_id"] == site_id), sites[0])
    verify_site_ownership(entry["site_id"], current_user, request)
    invoices = visible_invoices(entry["site_id"], current_user)
    tickets = [cockpit.shape_ticket(t, {entry["site_id"]: entry["name"]}, reader_role="client")
               for t in db.list_tickets(site_id=entry["site_id"])]
    availability = telemetry_engine.get_site_availability(entry["site_id"])
    return {
        **base,
        "site": client_view(entry),
        "traffic": telemetry_engine.get_traffic(entry["site_id"]),
        "availability": {k: availability.get(k) for k in ("series", "uptime", "applicable", "current_state")},
        "invoices": invoices[:5],
        "invoice_totals": cockpit.invoice_totals(invoices),
        "tickets": tickets[:5],
        "pending_tickets": sum(1 for t in tickets if t["status"] != "closed"),
    }


@app.get("/api/v1/sites/{site_id}/invoices", tags=["Client Portal"])
def client_site_invoices(
    site_id: str,
    site: Annotated[dict[str, Any], Depends(verify_site_ownership)],
    current_user: Annotated[dict[str, Any], Depends(get_current_user)],
) -> dict[str, Any]:
    invoices = visible_invoices(site_id, current_user)
    return {"site_id": site_id, "invoices": invoices, "totals": cockpit.invoice_totals(invoices)}


@app.get("/api/v1/invoices/{invoice_id}", tags=["Client Portal"])
def client_invoice_receipt(
    invoice_id: str,
    request: Request,
    current_user: Annotated[dict[str, Any], Depends(get_current_user)],
) -> dict[str, Any]:
    invoice = db.get_invoice(invoice_id)
    if not invoice or not invoice.get("site_id"):
        raise HTTPException(status_code=404, detail="Facture introuvable.")
    verify_site_ownership(str(invoice["site_id"]), current_user, request)
    if invoice.get("status") == "draft" and current_user.get("role") != "admin":
        raise HTTPException(status_code=404, detail="Facture introuvable.")
    return receipt_payload(invoice)


def owned_ticket(ticket_id: str, current_user: dict[str, Any], request: Request) -> dict[str, Any]:
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Conversation introuvable.")
    verify_site_ownership(str(ticket.get("site_id") or ""), current_user, request)
    return ticket


def reader_role(current_user: dict[str, Any]) -> str:
    return "agency" if current_user.get("role") == "admin" else "client"


@app.get("/api/v1/sites/{site_id}/tickets", tags=["Client Portal"])
def client_site_tickets(
    site_id: str,
    site: Annotated[dict[str, Any], Depends(verify_site_ownership)],
) -> dict[str, Any]:
    names = {site_id: site.get("name") or site_id}
    tickets = [cockpit.shape_ticket(t, names, reader_role="client") for t in db.list_tickets(site_id=site_id)]
    return {"site_id": site_id, "tickets": tickets, "schema_ready": db.relation_ready(db.TABLE_TICKETS)}


@app.post("/api/v1/sites/{site_id}/tickets", tags=["Client Portal"], status_code=201)
def client_create_ticket(
    site_id: str,
    payload: TicketCreateRequest,
    site: Annotated[dict[str, Any], Depends(verify_site_ownership)],
    current_user: Annotated[dict[str, Any], Depends(get_current_user)],
) -> dict[str, Any]:
    ticket = db.create_ticket(site_id, payload.subject.strip(), payload.body.strip(),
                              author_label(current_user), reader_role(current_user),
                              payload.priority)
    return {"ticket": cockpit.shape_ticket(ticket, {site_id: site.get("name") or site_id},
                                           reader_role=reader_role(current_user), with_messages=True)}


@app.get("/api/v1/tickets/{ticket_id}", tags=["Client Portal"])
def client_get_ticket(
    ticket_id: str,
    request: Request,
    current_user: Annotated[dict[str, Any], Depends(get_current_user)],
) -> dict[str, Any]:
    ticket = owned_ticket(ticket_id, current_user, request)
    db.mark_ticket_seen(ticket_id, reader_role(current_user))
    return {"ticket": cockpit.shape_ticket(ticket, site_names(), reader_role=reader_role(current_user),
                                           with_messages=True)}


@app.post("/api/v1/tickets/{ticket_id}/messages", tags=["Client Portal"])
def client_reply_ticket(
    ticket_id: str,
    payload: TicketReplyRequest,
    request: Request,
    current_user: Annotated[dict[str, Any], Depends(get_current_user)],
) -> dict[str, Any]:
    owned_ticket(ticket_id, current_user, request)
    db.add_ticket_message(ticket_id, payload.body.strip(), author_label(current_user), reader_role(current_user))
    return {"ticket": cockpit.shape_ticket(require_ticket(ticket_id), site_names(),
                                           reader_role=reader_role(current_user), with_messages=True)}


class ClientTicketStatusRequest(BaseModel):
    status: Literal["open", "closed"]


@app.post("/api/v1/tickets/{ticket_id}/status", tags=["Client Portal"])
def client_set_ticket_status(
    ticket_id: str,
    payload: ClientTicketStatusRequest,
    request: Request,
    current_user: Annotated[dict[str, Any], Depends(get_current_user)],
) -> dict[str, Any]:
    owned_ticket(ticket_id, current_user, request)
    db.set_ticket_status(ticket_id, payload.status)
    return {"ticket": cockpit.shape_ticket(require_ticket(ticket_id), site_names(),
                                           reader_role=reader_role(current_user), with_messages=True)}


# =========================================================
# 4.8 PAGE DE STATUT PUBLIQUE (/status) — AGRÉGATS UNIQUEMENT
# =========================================================

PUBLIC_CORE_SERVICES = (
    ("core:api", "Cockpit & API"),
    ("core:database", "Base de données"),
    ("core:docker", "Hébergement des vitrines"),
)


#: Route publique sans authentification : réponse mutualisée quelques secondes, pour
#: qu'un rafraîchissement en boucle ne recalcule pas 90 jours de SLA à chaque appel.
PUBLIC_STATUS_TTL_SECONDS = 10.0
_PUBLIC_STATUS_CACHE: dict[str, Any] = {"at": 0.0, "value": None}
_PUBLIC_STATUS_LOCK = threading.Lock()


@app.get("/api/status", tags=["Public"])
def public_status() -> dict[str, Any]:
    """Statut public : services de la plateforme et vitrines agrégées (aucun identifiant client)."""
    with _PUBLIC_STATUS_LOCK:
        now = time.monotonic()
        cached = _PUBLIC_STATUS_CACHE["value"]
        if cached is None or now - _PUBLIC_STATUS_CACHE["at"] >= PUBLIC_STATUS_TTL_SECONDS:
            cached = build_public_status()
            _PUBLIC_STATUS_CACHE.update(at=now, value=cached)
        return cached


def build_public_status() -> dict[str, Any]:
    health = telemetry_engine.peek_fleet_health()
    uptime = telemetry_engine.uptime
    services = []
    for key, label in PUBLIC_CORE_SERVICES:
        summary = uptime.summary(key)
        services.append({"label": label, "state": summary["current_state"], "uptime": summary["uptime"],
                         "avg_latency_ms": summary["avg_latency_ms"], "series": summary["series"]})
    site_keys = uptime.keys("site:")
    sites_series, sites_uptime = uptime.global_series(site_keys)
    fleet = health.get("fleet") or {}
    running = int(fleet.get("running") or 0)
    unreachable = int(fleet.get("http_unreachable") or 0) + int(fleet.get("restarting_crashloop") or 0)
    services.append({
        "label": "Vitrines clientes",
        "state": "down" if unreachable and not running else ("degraded" if unreachable else ("up" if running else "none")),
        "uptime": sites_uptime, "avg_latency_ms": None, "series": sites_series,
        "detail": f"{running} en ligne" + (f" · {unreachable} en anomalie" if unreachable else ""),
    })
    status_map = {"HEALTHY": "operational", "DEGRADED": "degraded", "CRITICAL": "outage"}
    sla = telemetry_engine.get_sla()
    return {
        "status": status_map.get(str(health.get("status") or ""), "unknown"),
        "updated_at": health.get("cached_at"),
        "uptime": sla["uptime"],
        "series": sla["series"],
        "services": services,
    }


# ---------------------------------------------------------
# 5. PLAN DE DONNÉES DES CONTENEURS TENANTS (JETON SCOPÉ, ZÉRO SECRET PARTAGÉ)
# ---------------------------------------------------------
#
# Historique de sécurité : les conteneurs tenants recevaient auparavant
# SUPABASE_URL + SUPABASE_KEY (service_role). Toute exécution de code dans un
# conteneur locataire permettait donc de lire/écrire/supprimer les données de
# l'ensemble de la flotte (contournement total du RLS). Désormais le conteneur
# ne détient qu'un jeton HMAC scopé à son tenant et n'accède à la base qu'à
# travers ces deux endpoints, qui imposent le tenant_id côté serveur.

SYNC_TOKEN_HEADER = "X-WAAS-Sync-Token"
MAX_SYNC_RECORDS = 2000


def require_sync_tenant(
    request: Request,
    x_waas_sync_token: Annotated[str | None, Header(alias=SYNC_TOKEN_HEADER)] = None,
) -> str:
    """Authentifie un conteneur locataire et retourne **son** tenant_id."""
    tenant_id = sync_auth.verify_tenant_token(x_waas_sync_token)
    if not tenant_id:
        client_host = request.client.host if request.client else "inconnue"
        logger.warning(
            "Jeton de synchronisation invalide ou absent (source=%s, chemin=%s)",
            client_host, request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Jeton de synchronisation invalide.",
        )
    return tenant_id


class SyncRecord(BaseModel):
    file_path: str = Field(..., min_length=1, max_length=512)
    xpath: str = Field(..., min_length=1, max_length=2048)
    content_type: str = Field(default="text", max_length=16)
    content_value: str = Field(..., max_length=5000)


class SyncPushRequest(BaseModel):
    records: list[SyncRecord] = Field(..., max_length=MAX_SYNC_RECORDS)


@app.get("/internal/sync/content", include_in_schema=False, tags=["Internal"])
def internal_sync_content(
    tenant_id: Annotated[str, Depends(require_sync_tenant)],
) -> dict[str, Any]:
    """Renvoie le contenu du locataire **désigné par le jeton** (lecture seule, champs minimaux).

    Lecture stricte : une base injoignable répond 502 au lieu d'une liste vide, qui
    ferait servir la vitrine sans le contenu du client (moteur v1 : « premier lancement »).
    """
    try:
        records = db.get_tenant_content(tenant_id, strict=True)
    except db.ContentStoreError as exc:
        logger.error("%s", exc)
        raise HTTPException(status_code=502, detail="Contenu indisponible.") from None
    projected = [
        {
            "file_path": r.get("file_path"),
            "xpath": r.get("xpath"),
            "content_value": r.get("content_value"),
        }
        for r in records
        if isinstance(r, dict) and r.get("file_path") and r.get("xpath")
    ]
    return {"tenant_id": tenant_id, "total": len(projected), "records": projected}


@app.post("/internal/sync/content", include_in_schema=False, tags=["Internal"])
def internal_sync_content_push(
    payload: SyncPushRequest,
    tenant_id: Annotated[str, Depends(require_sync_tenant)],
) -> dict[str, Any]:
    """Indexe les éléments **nouveaux** extraits par un conteneur, pour son seul tenant.

    Protocole v1 : une ligne existante n'est jamais écrasée (le travail du client
    prime sur le texte du gabarit renvoyé par le conteneur).
    """
    records = [r.model_dump() for r in payload.records if is_page_path(r.file_path)]
    if len(records) < len(payload.records):
        # Ignorés plutôt que refusés : un 422 ferait boucler un conteneur v1 au démarrage.
        logger.warning("Synchronisation '%s' : %d enregistrement(s) au chemin de page invalide ignoré(s).",
                       tenant_id, len(payload.records) - len(records))
    try:
        written = db.upsert_tenant_content(tenant_id, records)
    except db.ContentStoreError as exc:
        logger.error("%s", exc)
        raise HTTPException(status_code=502, detail="Enregistrement du contenu indisponible.") from None
    logger.info(
        "Synchronisation tenant '%s' : %d/%d enregistrements créés",
        tenant_id, written, len(payload.records),
    )
    return {"tenant_id": tenant_id, "upserted": written}


MAX_INVENTORY_SLOTS = 5000


def is_page_path(value: str) -> bool:
    """Chemin de page relatif à la racine du site, sans détour : le Studio en fait une
    adresse de navigation (« //hôte » ou « .. » le ferait sortir de la vitrine)."""
    if not value or value.startswith("/") or "\\" in value or any(ord(char) < 32 for char in value):
        return False
    return not any(part in ("", ".", "..") for part in value.split("/"))


class InventorySlotModel(BaseModel):
    xpath: str = Field(..., min_length=1, max_length=MAX_CONTENT_KEY_LENGTH)
    content_type: Literal["text", "image", "attr"] = "text"
    source: str = Field(default="", max_length=content_sync.MAX_VALUE_LENGTH)
    exact: bool = False                     # texte préformaté : les blancs comptent


class InventoryRequest(BaseModel):
    file_path: str = Field(..., min_length=1, max_length=MAX_CONTENT_PATH_LENGTH)
    slots: list[InventorySlotModel] = Field(default_factory=list, max_length=MAX_INVENTORY_SLOTS)
    engine: int = Field(default=2, ge=1, le=99)

    @field_validator("file_path")
    @classmethod
    def _relative_page_path(cls, value: str) -> str:
        if not is_page_path(value):
            raise ValueError("chemin de page invalide (relatif à la racine du site attendu)")
        return value


@app.post("/internal/sync/inventory", include_in_schema=False, tags=["Internal"])
def internal_sync_inventory(
    payload: InventoryRequest,
    tenant_id: Annotated[str, Depends(require_sync_tenant)],
) -> dict[str, Any]:
    """Protocole v2 : réconcilie l'inventaire d'une page avec le contenu du locataire.

    Le conteneur décrit les emplacements éditables de son gabarit ; l'API indexe les
    nouveaux, suit les éléments déplacés, détache les orphelins (voir
    ``content_sync``) et renvoie les seules valeurs à injecter. Tout est scopé au
    tenant **du jeton** et à la page indiquée.
    """
    slots = [content_sync.InventorySlot(slot.xpath, slot.content_type, slot.source, slot.exact)
             for slot in payload.slots]
    try:
        # Fermé en cas de doute : sans certitude sur les ancres, pas de publication par clé seule.
        anchors = db.content_anchors_ready(strict=True)
        rows = db.get_tenant_content(tenant_id, payload.file_path, strict=True)
        plan = content_sync.reconcile(slots, [content_sync.record_from_row(row) for row in rows], anchors)
        if plan.writes:
            db.apply_content_plan(tenant_id, payload.file_path, plan, anchors)
    except db.ContentStoreError as exc:
        logger.error("%s", exc)
        raise HTTPException(status_code=502, detail="Contenu indisponible.") from None
    stats = plan.stats()
    if plan.writes:
        logger.info("Réconciliation '%s' / %s : %s", tenant_id, payload.file_path, stats)
    return {
        "protocol": 2,
        "tenant_id": tenant_id,
        "file_path": payload.file_path,
        "anchors": anchors,
        "values": [{"xpath": key, "content_value": value} for key, value in plan.placements.items()],
        "stats": stats,
    }


SOURCE_NONCE_HEADER = "X-WAAS-Source-Nonce"


def tenant_source_repository(tenant_id: str, nonce: Optional[str]) -> git_source.GithubRepo:
    """Dépôt **provisionné** du conteneur qui s'authentifie (jeton + clé ``SOURCE_NONCE``).

    Jamais lu depuis la requête : un conteneur ne lit que son propre dépôt. La clé
    aléatoire du conteneur courant est exigée en plus du jeton HMAC, qui est
    déterministe et permanent : un jeton seul (forgé avec un secret par défaut, ou
    hérité d'un conteneur détruit dont l'identifiant a été réattribué) ne suffit pas.
    """
    if docker_client is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Daemon Docker inaccessible.")
    try:
        env = container_environment(docker_client.containers.get(f"waas-{tenant_id}"))
    except NotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Aucun conteneur pour ce locataire.") from None
    except APIError as exc:
        logger.warning("Conteneur de '%s' illisible : %s", tenant_id, exc)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Conteneur illisible.") from None
    expected = env.get("SOURCE_NONCE") or ""
    if not expected or not hmac.compare_digest(expected.encode("utf-8"), (nonce or "").encode("utf-8")):
        logger.warning("Clé de source invalide pour le locataire '%s'.", tenant_id)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Clé de source invalide.")
    repo = git_source.github_repository(validate_repo_url(env.get("REPO_URL") or ""))
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Dépôt hors GitHub : il est cloné par le conteneur lui-même (SOURCE_MODE=git).",
        )
    return repo


@app.get("/internal/sync/source", include_in_schema=False, tags=["Internal"])
def internal_sync_source(
    tenant_id: Annotated[str, Depends(require_sync_tenant)],
    x_waas_source_nonce: Annotated[str | None, Header(alias=SOURCE_NONCE_HEADER)] = None,
) -> StreamingResponse:
    """Archive ``tar.gz`` du dépôt du conteneur appelant (``SOURCE_MODE=api``).

    Téléchargée avec le jeton GitHub de la plateforme, qui ne quitte jamais l'API :
    c'est ce qui permet d'héberger des dépôts privés sans identifiant dans le conteneur.
    L'admission (quota, exclusivité par locataire) précède toute lecture Docker.
    """
    try:
        with git_source.relay_gate.slot(tenant_id):
            repo = tenant_source_repository(tenant_id, x_waas_source_nonce)
            archive, size = git_source.download_archive(repo, git_source.github_token())
    except git_source.SourceError as exc:
        logger.error("Archive du locataire '%s' indisponible : %s", tenant_id, exc)
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    logger.info("Archive de %s (%d octets) remise au locataire '%s'.", repo.slug, size, tenant_id)
    return StreamingResponse(git_source.iter_file(archive), media_type="application/gzip",
                             headers={"Content-Length": str(size), "Cache-Control": "no-store"})


# ---------------------------------------------------------
# ROUTES DU FRONTEND (SECTION ADMIN CONFINÉE & CATCH-ALL PUBLIC)
# ---------------------------------------------------------

_ASSET_VERSION: dict[str, Any] = {"value": "", "at": 0.0}


def asset_version() -> str:
    """Empreinte des CSS/JS communs (dates de modification), relue au plus toutes les 3 s :
    toute mise à jour change les URL ``?v=…`` et invalide le cache des navigateurs."""
    now = time.monotonic()
    if _ASSET_VERSION["value"] and now - _ASSET_VERSION["at"] < 3.0:
        return _ASSET_VERSION["value"]
    stamps = [
        f"{path.relative_to(FRONT_DIR).as_posix()}:{path.stat().st_mtime_ns}"
        for folder in ("css", "js") if (FRONT_DIR / folder).is_dir()
        for path in sorted((FRONT_DIR / folder).rglob("*")) if path.is_file()   # js/studio/ compris
    ]
    _ASSET_VERSION.update(value=hashlib.sha256("|".join(stamps).encode("utf-8")).hexdigest()[:10], at=now)
    return _ASSET_VERSION["value"]


def resolve_includes(html_text: str, params: Optional[dict[str, str]] = None) -> str:
    """Inclusions ``<!-- @include … -->`` **confinées à Front/components** (voir pages.py)."""
    return pages.resolve_includes(html_text, COMPONENTS_DIR, {"v": asset_version(), **(params or {})})


def locate_page(root: Path, subpath: str) -> Optional[Path]:
    """Dossier -> ``index.html`` (1 page par endpoint), confiné à ``root``, sans fichier caché."""
    clean = subpath.strip("/")
    if any(part.startswith(".") for part in clean.split("/") if part):
        return None
    target = (root / clean).resolve() if clean else root.resolve()
    if target.is_dir():
        target = (target / "index.html").resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    if target.is_relative_to(COMPONENTS_DIR):
        return None
    return target


def account_initials(username: str) -> str:
    """Initiales du compte, **identiques** à ``UI.initials`` côté client.

    La coque est ainsi rendue juste par le serveur à la première peinture, et le
    rafraîchissement JSON qui suit ne change plus la valeur affichée.
    """
    parts = [part for part in re.split(r"[\s._@-]+", username or "") if part]
    return ("".join(part[0] for part in parts[:2]) or "?").upper()


def shell_params(user: Optional[dict[str, Any]], default_username: str) -> dict[str, str]:
    """Identité injectée dans la coque (sidebar) : peinte avec la page, sans attente réseau."""
    username = str((user or {}).get("username") or "").strip() or default_username
    return {"shell_username": username, "shell_avatar": account_initials(username)}


async def render_page(target: Path, params: Optional[dict[str, str]] = None) -> Response:
    """Page HTML : inclusions résolues, identité du compte injectée, jamais mise en cache."""
    if target.suffix.lower() != ".html":
        return FileResponse(target)
    rendered = resolve_includes(target.read_text(encoding="utf-8"), params)
    # Réglage lu hors event loop (cache Supabase rafraîchi périodiquement).
    api_public_url = await asyncio.to_thread(api_public_url_setting)
    if api_public_url and "window.API_URL" not in rendered:
        # Sérialisation JSON + « < » échappé : la valeur vient de la base et ne doit ni
        # clore le littéral JS ni la balise (</script>) — injection de script persistante.
        api_script = f"<script>window.API_URL = {json.dumps(api_public_url).replace('<', '\\u003c')};</script>"
        rendered = rendered.replace("</head>", f"  {api_script}\n</head>", 1)
    return HTMLResponse(content=rendered, headers=NO_CACHE_HEADERS)


@app.get("/admin", include_in_schema=False)
@app.get("/admin/{subpath:path}", include_in_schema=False)
async def serve_admin_panel(
    subpath: str = "",
    _admin: dict[str, Any] = Depends(get_current_admin_from_cookie_or_header),
):
    """Console d'administration (/admin/<section>/<sous-section>) : administrateurs uniquement."""
    if not subpath.strip("/"):
        return RedirectResponse(url="/admin/dashboard", status_code=status.HTTP_302_FOUND)
    target = locate_page(ADMIN_DIR, subpath)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page ou ressource admin introuvable.")
    return await render_page(target, shell_params(_admin, "Administrateur"))


@app.get("/portal", include_in_schema=False)
@app.get("/portal/{subpath:path}", include_in_schema=False)
async def serve_client_portal(
    current_user: Annotated[dict[str, Any], Depends(get_current_user)],
    subpath: str = "",
):
    """Espace client (vue d'ensemble, factures, support) : tout compte authentifié.

    Les données restent filtrées par l'API (contrôle de propriété de chaque site).
    """
    target = locate_page(PORTAL_DIR, subpath)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page introuvable.")
    return await render_page(target, shell_params(current_user, "Client"))


@app.get("/studio", include_in_schema=False)
async def serve_studio(current_user: Annotated[dict[str, Any], Depends(get_current_user)]):
    """Studio d'édition visuelle d'une vitrine (agence et client propriétaire)."""
    target = locate_page(STUDIO_DIR, "")
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page introuvable.")
    return await render_page(target)


@app.get("/", include_in_schema=False)
@app.get("/{full_path:path}", include_in_schema=False)
async def serve_public_frontend(full_path: str = ""):
    """Catch-all pour les pages publiques (connexion, statut) et les assets, confinement strict."""
    clean = full_path.strip("/")
    first = clean.split("/", 1)[0].lower()

    # Zones protégées (servies par leurs routes dédiées) et fragments : jamais par ce canal.
    if first in ("admin", "portal", "studio", "components"):
        if first == "admin":
            log_masked_forbidden(
                f"/{clean}",
                "Tentative d'accès au répertoire d'administration via la route publique",
                underlying_code=403,
            )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page introuvable.")

    # Blocage préventif des fichiers de configuration
    if Path(clean).name.lower() in ("package.json", "package-lock.json", "tsconfig.json", "pyproject.toml", "uv.lock"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ressource introuvable.")

    target = locate_page(FRONT_DIR, clean or "login")
    # Re-vérification après résolution : « login/../admin » ne doit jamais atteindre une zone protégée.
    if target is None or any(target.is_relative_to(zone) for zone in PROTECTED_DIRS):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page introuvable.")
    return await render_page(target)