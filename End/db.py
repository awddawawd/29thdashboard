import logging
import mimetypes
import os
import threading
import time
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional
from supabase import Client, create_client

import content_sync

from pathlib import Path
try:
    from dotenv import load_dotenv
    for _env_file in (
        Path(".env"),
        Path(__file__).parent / ".env",
        Path(__file__).parent.parent / ".env",
    ):
        if _env_file.is_file():
            load_dotenv(_env_file, override=False)
            break
except ImportError:
    pass

# ---------------------------------------------------------
# CLIENT SUPABASE & CONSTANTES
# ---------------------------------------------------------

def _clean_key(val: Optional[str]) -> Optional[str]:
    if not val:
        return None
    cleaned = val.strip()
    if "•" in cleaned or "..." in cleaned:
        return None
    return cleaned

SUPABASE_URL = os.getenv("SUPABASE_URL")
# Nouvelle nomenclature Supabase (sb_secret_...) ou ancienne (service_role)
SUPABASE_KEY = (
    _clean_key(os.getenv("SUPABASE_SECRET_KEY"))
    or _clean_key(os.getenv("SUPABASE_SERVICE_KEY"))
    or _clean_key(os.getenv("SUPABASE_SERVICE_ROLE_KEY"))
    or _clean_key(os.getenv("SUPABASE_KEY"))
)
SUPABASE_SECRET_KEY = SUPABASE_KEY

# Nouvelle clé publiable (sb_publishable_...) ou ancienne (anon)
SUPABASE_PUBLISHABLE_KEY = (
    _clean_key(os.getenv("SUPABASE_PUBLISHABLE_KEY"))
    or _clean_key(os.getenv("SUPABASE_ANON_KEY"))
)

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError(
        "Configuration critique manquante : SUPABASE_URL et SUPABASE_SECRET_KEY "
        "(ou SUPABASE_SERVICE_KEY) sont obligatoires. "
        "Définissez-les dans le fichier .env ou dans l'environnement."
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

logger = logging.getLogger("waas.db")

# Synchronisation thread-safe pour les clients synchrones Supabase (httpx/httpcore).
# Sous Windows, des requêtes simultanées partageant un pool de connexions TLS peuvent
# déclencher [WinError 10035] (WSAEWOULDBLOCK) car httpcore lève ReadError sur les
# sockets non-bloquants lors d'accès concurrents (télémétrie + endpoints API).
_session_lock = threading.RLock()


def _install_thread_safe_session(client: Client) -> None:
    postgrest_session = getattr(getattr(client, "postgrest", None), "session", None)
    if postgrest_session and hasattr(postgrest_session, "request"):
        orig_pg_request = postgrest_session.request

        def _safe_postgrest_request(*args: Any, **kwargs: Any) -> Any:
            with _session_lock:
                for attempt in range(3):
                    try:
                        return orig_pg_request(*args, **kwargs)
                    except Exception as exc:
                        is_socket_block = "10035" in str(exc) or "ReadError" in type(exc).__name__
                        if is_socket_block and attempt < 2:
                            time.sleep(0.05 * (attempt + 1))
                            continue
                        raise

        postgrest_session.request = _safe_postgrest_request

    storage_session = getattr(getattr(client, "storage", None), "session", None)
    if storage_session and hasattr(storage_session, "request"):
        orig_storage_request = storage_session.request

        def _safe_storage_request(*args: Any, **kwargs: Any) -> Any:
            with _session_lock:
                for attempt in range(3):
                    try:
                        return orig_storage_request(*args, **kwargs)
                    except Exception as exc:
                        is_socket_block = "10035" in str(exc) or "ReadError" in type(exc).__name__
                        if is_socket_block and attempt < 2:
                            time.sleep(0.05 * (attempt + 1))
                            continue
                        raise

        storage_session.request = _safe_storage_request


_install_thread_safe_session(supabase)

TABLE_USERS = "waas_users"
TABLE_CONTENT = "tenant_content"
TABLE_SETTINGS = "fleet_settings"
TABLE_ANALYTICS = "tenant_analytics"
TABLE_MONITORS = "tenant_monitors"

# Tables du cockpit (migration End/migrations/001_cockpit.sql)
TABLE_INVOICES = "invoices"
TABLE_TICKETS = "tickets"
TABLE_TICKET_MESSAGES = "ticket_messages"
TABLE_UPTIME = "uptime_daily"
TABLE_HOST_HOURLY = "host_metrics_hourly"

MIGRATION_FILE = "End/migrations/001_cockpit.sql"


# ---------------------------------------------------------
# 0. SCHÉMA : DÉTECTION DES TABLES ABSENTES (MIGRATION NON APPLIQUÉE)
# ---------------------------------------------------------

class SchemaNotReady(RuntimeError):
    """Une table ou une fonction de la migration du cockpit est absente de Supabase."""

    def __init__(self, relation: str) -> None:
        self.relation = relation
        super().__init__(
            f"Migration Supabase requise : '{relation}' est absente. "
            f"Exécutez {MIGRATION_FILE} dans l'éditeur SQL de Supabase."
        )


class DuplicateRecord(ValueError):
    """Violation d'unicité (identifiant ou numéro déjà utilisé)."""


#: Relation absente -> instant (monotonic) à partir duquel on retente. Évite de
#: marteler Supabase avec des requêtes vouées à l'échec, sans exiger de redémarrage
#: une fois la migration appliquée.
_MISSING: dict[str, float] = {}
_MISSING_LOCK = threading.Lock()
_MISSING_RETRY_SECONDS = 60.0


def _error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if code:
        return str(code)
    text = str(exc)
    for candidate in ("PGRST205", "PGRST202", "PGRST204", "42P01", "42703", "42883", "23505"):
        if candidate in text:
            return candidate
    return ""


def _is_missing_relation(exc: BaseException) -> bool:
    return _error_code(exc) in ("PGRST205", "PGRST202", "42P01", "42883")


def _mark_missing(relation: str) -> None:
    with _MISSING_LOCK:
        first_time = relation not in _MISSING
        _MISSING[relation] = time.monotonic() + _MISSING_RETRY_SECONDS
    if first_time:
        logger.warning("Relation Supabase '%s' absente : appliquez %s.", relation, MIGRATION_FILE)


def _mark_present(relation: str) -> None:
    with _MISSING_LOCK:
        _MISSING.pop(relation, None)


def relation_ready(relation: str) -> bool:
    """Faux tant qu'une relation a été vue absente récemment (nouvel essai après 60 s)."""
    with _MISSING_LOCK:
        retry_at = _MISSING.get(relation)
    return retry_at is None or time.monotonic() >= retry_at


def _read(relation: str, query: Any, default: Any, strict: bool = False) -> Any:
    """Exécute une lecture ; une relation absente renvoie ``default`` sans bruit.

    ``strict`` : toute autre erreur est relevée au lieu de renvoyer ``default``. À
    utiliser quand un résultat vide changerait une décision d'accès (fail-closed).
    """
    if not relation_ready(relation):
        return default
    try:
        res = query.execute()
    except Exception as exc:
        if _is_missing_relation(exc):
            _mark_missing(relation)
        elif strict:
            raise
        else:
            logger.warning("Lecture '%s' impossible : %s", relation, exc)
        return default
    _mark_present(relation)
    return res.data if res.data is not None else default


def _write(relation: str, query: Any) -> list[dict[str, Any]]:
    """Exécute une écriture ; relation absente -> SchemaNotReady, doublon -> DuplicateRecord."""
    if not relation_ready(relation):
        raise SchemaNotReady(relation)
    try:
        res = query.execute()
    except Exception as exc:
        if _is_missing_relation(exc):
            _mark_missing(relation)
            raise SchemaNotReady(relation) from exc
        if _error_code(exc) == "23505":
            raise DuplicateRecord(str(getattr(exc, "message", exc))) from exc
        raise
    _mark_present(relation)
    return [row for row in (res.data or []) if isinstance(row, dict)]


def missing_relations() -> list[str]:
    """Relations vues absentes lors des dernières requêtes (sans nouvel appel réseau)."""
    with _MISSING_LOCK:
        return sorted(_MISSING)


def probe_schema() -> dict[str, bool]:
    """Présence de chaque table du cockpit (requête minimale, limite 1)."""
    report: dict[str, bool] = {}
    for table in (
        TABLE_SITES, TABLE_INVOICES, TABLE_TICKETS, TABLE_TICKET_MESSAGES,
        TABLE_UPTIME, TABLE_HOST_HOURLY,
    ):
        try:
            supabase.table(table).select("*").limit(1).execute()
            _mark_present(table)
            report[table] = True
        except Exception as exc:
            if _is_missing_relation(exc):
                _mark_missing(table)
            report[table] = False
    return report


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------
# 1. AUTHENTIFICATION & UTILISATEURS
# ---------------------------------------------------------

def authenticate_user(username: str, password: str) -> Optional[dict[str, Any]]:
    """Vérifie le mot de passe via la fonction RPC pgcrypto de Supabase."""
    try:
        res = supabase.rpc("verify_user_credentials", {
            "p_username": username,
            "p_password": password,
        }).execute()
        if res.data and len(res.data) > 0:
            return res.data[0]
    except Exception as e:
        print(f"[DB Error] authenticate_user: {e}")
    return None


def get_user_by_username(username: str) -> Optional[dict[str, Any]]:
    """Récupère le profil d'un utilisateur par son nom d'utilisateur."""
    try:
        res = (
            supabase.table(TABLE_USERS)
            .select("id, username, role, tenant_id")
            .eq("username", username)
            .limit(1)
            .execute()
        )
        if res.data and len(res.data) > 0:
            return res.data[0]
    except Exception as e:
        print(f"[DB Error] get_user_by_username: {e}")
    return None


# ---------------------------------------------------------
# 1.1 GESTION MULTI-SITES (1 CLIENT = N SITES)
# ---------------------------------------------------------

TABLE_SITES = "sites"


def _legacy_site(tenant_id: str, user: dict[str, Any]) -> dict[str, Any]:
    """Site déduit de l'ancien rattachement ``waas_users.tenant_id`` (site sans fiche)."""
    return {
        "site_id": tenant_id,
        "id": tenant_id,
        "client_id": user.get("id"),
        "name": tenant_id,
        "domain": f"{tenant_id}.local",
        "created_at": user.get("created_at"),
        "client_username": user.get("username"),
    }


def get_sites_by_client_id(client_id: str) -> list[dict[str, Any]]:
    """Sites d'un client (1 Client = N Sites).

    Propriété à **source unique** : la fiche ``sites`` fait foi ; l'ancien
    ``waas_users.tenant_id`` n'est retenu que si ce site n'a **aucune** fiche
    (sinon un rattachement périmé donnerait accès au site d'un autre client).
    """
    rows = _read(TABLE_SITES, supabase.table(TABLE_SITES).select("*").eq("client_id", client_id), [], strict=True)
    sites = [{**row, "site_id": row.get("id")} for row in rows if isinstance(row, dict)]

    user = get_user_by_id(client_id)
    tenant_id = str((user or {}).get("tenant_id") or "")
    if tenant_id and all(site["site_id"] != tenant_id for site in sites) and get_site_record(tenant_id) is None:
        sites.append(_legacy_site(tenant_id, user or {}))
    return sites


def get_site_by_id(site_id: str) -> Optional[dict[str, Any]]:
    """Site par identifiant ; ``client_id`` suit la même règle de propriété unique."""
    # 1. La fiche fait foi (client_id vide = site non attribué)
    record = get_site_record(site_id)
    if record:
        return record

    # 2. Site sans fiche : ancien rattachement waas_users.tenant_id
    rows = _read(
        TABLE_USERS,
        supabase.table(TABLE_USERS).select("id, username, tenant_id, created_at").eq("tenant_id", site_id).limit(1),
        [],
    )
    if rows and isinstance(rows[0], dict):
        return _legacy_site(site_id, rows[0])

    # 3. Instance Docker ou tenant orphelin
    return {
        "site_id": site_id,
        "id": site_id,
        "client_id": None,
        "name": site_id,
    }


def get_all_clients() -> list[dict[str, Any]]:
    """Récupère l'ensemble des comptes clients avec la liste de leurs sites associés.

    Optimisation : un seul balayage de la table ``sites`` (au lieu d'une requête
    par client) ; le repli historique sur ``waas_users.tenant_id`` est calculé à
    partir de la ligne déjà chargée, sans requête supplémentaire.
    """
    try:
        res = (
            supabase.table(TABLE_USERS)
            .select("id, username, role, tenant_id, created_at")
            .eq("role", "client")
            .execute()
        )
        clients = res.data or []
    except Exception as e:
        logger.error("get_all_clients: %s", e)
        return []

    sites_by_client: dict[str, list[dict[str, Any]]] = {}
    record_ids: set[str] = set()
    for row in list_site_records():
        record_ids.add(str(row.get("site_id")))
        owner = row.get("client_id")
        if owner is None:
            continue
        sites_by_client.setdefault(str(owner), []).append(row)

    for c in clients:
        if not isinstance(c, dict):
            continue
        sites = list(sites_by_client.get(str(c.get("id"))) or [])
        tenant_id = str(c.get("tenant_id") or "")
        # Même règle que get_sites_by_client_id : l'ancien rattachement ne vaut que sans fiche.
        if tenant_id and tenant_id not in record_ids:
            sites.append(_legacy_site(tenant_id, c))
        c["sites"] = sites
        c["sites_count"] = len(sites)

    return clients


def assign_site_to_client(site_id: str, client_id: str) -> bool:
    """Rattache un site à un client."""
    try:
        return bool(upsert_site_record(site_id, {"client_id": client_id}))
    except Exception:
        # Fallback waas_users si la table sites n'existe pas encore
        try:
            supabase.table(TABLE_USERS).update({"tenant_id": site_id}).eq("id", client_id).execute()
            return True
        except Exception as e:
            print(f"[DB Error] assign_site_to_client fallback: {e}")
            return False


# ---------------------------------------------------------
# 1.2 FICHES COMMERCIALES DES SITES (TABLE `sites`)
# ---------------------------------------------------------

#: Colonnes modifiables d'une fiche site (le reste est géré par la base).
SITE_FIELDS = (
    "client_id", "name", "plan_id", "plan_name", "price_monthly", "domain", "domain_registrar",
    "domain_expires_at", "ssl_expires_at", "contract_since", "contract_status", "repo_url", "notes",
)


def list_site_records() -> list[dict[str, Any]]:
    """Toutes les fiches de la table ``sites`` (vide si la migration n'est pas appliquée)."""
    rows = _read(TABLE_SITES, supabase.table(TABLE_SITES).select("*").order("name"), [])
    return [{**row, "site_id": row.get("id")} for row in rows if isinstance(row, dict)]


def get_site_record(site_id: str) -> Optional[dict[str, Any]]:
    """Fiche réelle d'un site (jamais synthétisée), ou ``None``.

    Lecture stricte : une erreur transitoire ne doit pas se lire « pas de fiche »,
    ce qui réactiverait un ancien rattachement ``tenant_id`` (propriété).
    """
    rows = _read(TABLE_SITES, supabase.table(TABLE_SITES).select("*").eq("id", site_id).limit(1), [], strict=True)
    if rows and isinstance(rows[0], dict):
        return {**rows[0], "site_id": rows[0].get("id")}
    return None


def upsert_site_record(site_id: str, fields: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Crée ou complète la fiche d'un site (seules les colonnes connues sont écrites)."""
    payload = {key: value for key, value in fields.items() if key in SITE_FIELDS}
    payload.update({"id": site_id, "updated_at": _now_iso()})
    rows = _write(TABLE_SITES, supabase.table(TABLE_SITES).upsert(payload, on_conflict="id"))
    return {**rows[0], "site_id": rows[0].get("id")} if rows else None


def delete_site_record(site_id: str) -> bool:
    try:
        return bool(_write(TABLE_SITES, supabase.table(TABLE_SITES).delete().eq("id", site_id)))
    except SchemaNotReady:
        return False


# ---------------------------------------------------------
# 1.3 COMPTES (CRÉATION, MOT DE PASSE, SUPPRESSION)
# ---------------------------------------------------------

RPC_CREATE_USER = "create_waas_user"
RPC_SET_PASSWORD = "set_waas_user_password"


def get_user_by_id(user_id: str) -> Optional[dict[str, Any]]:
    rows = _read(
        TABLE_USERS,
        supabase.table(TABLE_USERS).select("id, username, role, tenant_id, created_at").eq("id", user_id).limit(1),
        [],
    )
    return rows[0] if rows else None


def create_user(username: str, password: str, role: str = "client",
                tenant_id: Optional[str] = None) -> dict[str, Any]:
    """Crée un compte via la fonction SQL (hachage bcrypt côté base, jamais en clair)."""
    rows = _write(RPC_CREATE_USER, supabase.rpc(RPC_CREATE_USER, {
        "p_username": username,
        "p_password": password,
        "p_role": role,
        "p_tenant_id": tenant_id,
    }))
    if not rows:
        raise RuntimeError("Création du compte refusée par la base.")
    return rows[0]


def set_user_password(user_id: str, password: str) -> bool:
    if not relation_ready(RPC_SET_PASSWORD):
        raise SchemaNotReady(RPC_SET_PASSWORD)
    try:
        res = supabase.rpc(RPC_SET_PASSWORD, {"p_user_id": user_id, "p_password": password}).execute()
    except Exception as exc:
        if _is_missing_relation(exc):
            _mark_missing(RPC_SET_PASSWORD)
            raise SchemaNotReady(RPC_SET_PASSWORD) from exc
        raise
    return bool(res.data)


def update_user_tenant(user_id: str, tenant_id: Optional[str]) -> Optional[dict[str, Any]]:
    rows = _write(TABLE_USERS, supabase.table(TABLE_USERS).update({"tenant_id": tenant_id or None}).eq("id", user_id))
    return rows[0] if rows else None


def clear_tenant_refs(site_id: str, keep_user_id: Optional[str] = None) -> int:
    """Efface les anciens rattachements ``waas_users.tenant_id`` vers ``site_id``.

    Appelé quand la fiche désigne un (autre) propriétaire ou quand le site est
    purgé : la fiche fait déjà foi, ceci évite qu'un rattachement périmé ne
    ressurgisse si la fiche disparaît un jour.
    """
    query = supabase.table(TABLE_USERS).update({"tenant_id": None}).eq("tenant_id", site_id)
    if keep_user_id:
        query = query.neq("id", keep_user_id)
    return len(_write(TABLE_USERS, query))


def delete_user(user_id: str) -> bool:
    """Supprime un compte **client** (un administrateur ne se supprime jamais par l'API)."""
    rows = _write(TABLE_USERS, supabase.table(TABLE_USERS).delete().eq("id", user_id).eq("role", "client"))
    return bool(rows)


# ---------------------------------------------------------
# 2. CONTENU DU SITE (TENANT_CONTENT)
# ---------------------------------------------------------

#: Colonnes ajoutées par End/migrations/002_editor.sql. Facultatives : sans elles,
#: l'éditeur fonctionne sans ancres (relocalisation) ni audit des modifications.
EDITOR_MIGRATION_FILE = "End/migrations/002_editor.sql"
CONTENT_ANCHOR_COLUMN = "source_value"
CONTENT_AUDIT_COLUMNS = ("updated_at", "updated_by")
MAX_CONTENT_ROWS = 50_000
CONTENT_WRITE_CHUNK = 100
CONTENT_CONFLICT_KEY = "tenant_id,file_path,xpath"

_CONTENT_COLUMNS_PRESENT: set[str] = set()


class ContentStoreError(RuntimeError):
    """Contenu illisible ou non enregistré : la synchronisation doit échouer, jamais se taire
    (une vitrine servie sans le contenu du client est pire qu'un redémarrage)."""


def content_column_ready(column: str, strict: bool = False) -> bool:
    """Vrai si la colonne facultative existe (sondage mis en cache, nouvel essai après 60 s).

    ``strict`` : une erreur autre que « colonne absente » lève ``ContentStoreError`` au
    lieu de répondre faux — la réconciliation ne doit jamais se croire privée d'ancres
    à cause d'un incident réseau (elle publierait alors par clé, sans relocalisation).
    """
    relation = f"{TABLE_CONTENT}.{column}"
    if relation in _CONTENT_COLUMNS_PRESENT:
        return True
    if not relation_ready(relation):
        return False
    try:
        supabase.table(TABLE_CONTENT).select(column).limit(1).execute()
    except Exception as exc:
        if _error_code(exc) in ("42703", "PGRST204"):
            with _MISSING_LOCK:
                first_time = relation not in _MISSING
                _MISSING[relation] = time.monotonic() + _MISSING_RETRY_SECONDS
            if first_time:
                logger.warning("Colonne '%s' absente : appliquez %s (éditeur sans ancres ni audit).",
                               relation, EDITOR_MIGRATION_FILE)
            return False
        if strict:
            raise ContentStoreError(f"Sondage de la colonne '{relation}' impossible : {exc}") from exc
        logger.warning("Sondage de la colonne '%s' impossible : %s", relation, exc)
        return False
    _mark_present(relation)
    _CONTENT_COLUMNS_PRESENT.add(relation)
    return True


def content_anchors_ready(strict: bool = False) -> bool:
    return content_column_ready(CONTENT_ANCHOR_COLUMN, strict=strict)


def content_audit_ready() -> bool:
    return all(content_column_ready(column) for column in CONTENT_AUDIT_COLUMNS)


def _content_rows(tenant_id: str, file_path: Optional[str] = None) -> list[dict[str, Any]]:
    """Lecture **paginée et stricte** (PostgREST plafonne chaque réponse à 1000 lignes)."""
    rows: list[dict[str, Any]] = []
    while len(rows) < MAX_CONTENT_ROWS:
        query = supabase.table(TABLE_CONTENT).select("*").eq("tenant_id", tenant_id)
        if file_path is not None:
            query = query.eq("file_path", file_path)
        start = len(rows)
        try:
            res = query.order("id").range(start, start + PAGE_ROWS - 1).execute()
        except Exception as exc:
            raise ContentStoreError(f"Lecture du contenu de '{tenant_id}' impossible : {exc}") from exc
        page = [row for row in (res.data or []) if isinstance(row, dict)]
        rows.extend(page)
        if len(page) < PAGE_ROWS:
            break
    return rows


def get_tenant_content(tenant_id: str, file_path: Optional[str] = None,
                       strict: bool = False) -> list[dict[str, Any]]:
    """Contenu extrait d'un tenant (d'une page si ``file_path``), toutes pages de résultats.

    ``strict`` : une erreur lève ``ContentStoreError`` au lieu de renvoyer une liste
    vide — obligatoire pour la synchronisation des conteneurs.
    """
    try:
        return _content_rows(tenant_id, file_path)
    except ContentStoreError as exc:
        if strict:
            raise
        logger.warning("%s", exc)
        return []


def count_tenant_content(tenant_id: str) -> int:
    """Nombre d'éléments éditables d'un site (requête de comptage, sans transfert des lignes)."""
    try:
        res = (
            supabase.table(TABLE_CONTENT)
            .select("id", count="exact")
            .eq("tenant_id", tenant_id)
            .limit(1)
            .execute()
        )
        return int(res.count or 0)
    except Exception as exc:
        logger.warning("Comptage du contenu de %s impossible : %s", tenant_id, exc)
        return 0


def update_single_content(tenant_id: str, xpath: str, new_value: str,
                          file_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Met à jour un texte ou une URL d'image ciblé par son XPath (et sa page si connue).

    Sans ``file_path`` (anciens clients), toutes les pages partageant ce XPath sont
    modifiées : c'est le comportement historique, conservé pour compatibilité.
    """
    try:
        results = update_content_values(tenant_id, [{"xpath": xpath, "file_path": file_path,
                                                     "new_value": new_value}])
    except ContentStoreError as exc:
        logger.warning("%s", exc)
        return None
    result = results[0] if results else {}
    return result.get("row") if result.get("status") in ("updated", "unchanged") else None


def update_content_values(tenant_id: str, changes: list[dict[str, Any]],
                          author: Optional[str] = None) -> list[dict[str, Any]]:
    """Applique les modifications du Studio, avec contrôle de version optimiste.

    Chaque modification porte ``xpath``, ``new_value`` et, de préférence, ``file_path``
    et ``expected_value`` (valeur lue par le Studio). Statut par modification :
    ``updated`` · ``unchanged`` · ``not_found`` · ``conflict`` (la valeur en base a
    changé entre-temps : ``current_value`` est renvoyée) · ``invalid`` (``detail``).

    Avec ``expected_value``, l'écriture elle-même est gardée (date de dernière
    modification, ou valeur lue si elle est courte) : un enregistrement concurrent
    survenu entre la lecture et l'écriture devient un conflit, jamais un écrasement.
    Sans migration 002 et pour un texte de plus de ``GUARD_MAX_VALUE`` caractères, seule
    la comparaison préalable protège.
    """
    by_file: dict[Optional[str], list[dict[str, Any]]] = {}
    for file_path in {change.get("file_path") for change in changes}:
        by_file[file_path] = _content_rows(tenant_id, file_path)
    audit: dict[str, Any] = {}
    if content_audit_ready():
        audit = {"updated_at": datetime.now(timezone.utc).isoformat(), "updated_by": author}

    results: list[dict[str, Any]] = []
    for change in changes:
        xpath = change.get("xpath")
        rows = [row for row in by_file.get(change.get("file_path"), []) if row.get("xpath") == xpath]
        result, value = _change_outcome(change, rows)
        results.append(result)
        if value is None:
            continue
        guarded = change.get("expected_value") is not None
        updated: list[dict[str, Any]] = []
        for row in rows:
            written = _guarded_update(tenant_id, row, value, audit, guarded)
            if not written and guarded:
                result.update(status="conflict", current_value=_stored_value(tenant_id, row["id"]))
                break
            if written:
                # Les modifications suivantes du même lot comparent à la valeur écrite.
                row.update(content_value=value, **audit)
                updated.extend(written)
        if result.get("status") != "conflict":
            result.update(status="updated" if updated else "not_found", row=updated[0] if updated else None)
    return results


#: Au-delà, la valeur lue ne sert pas de garde d'écriture (le filtre passerait dans l'URL).
GUARD_MAX_VALUE = 1024


def _guarded_update(tenant_id: str, row: dict[str, Any], value: str, audit: dict[str, Any],
                    guarded: bool) -> list[dict[str, Any]]:
    query = (supabase.table(TABLE_CONTENT).update({"content_value": value, **audit})
             .eq("tenant_id", tenant_id).eq("id", row["id"]))
    if guarded:
        if "updated_at" in row:
            stamp = row.get("updated_at")
            query = query.eq("updated_at", stamp) if stamp is not None else query.is_("updated_at", "null")
        current = row.get("content_value")
        if current is None:
            query = query.is_("content_value", "null")
        elif len(str(current)) <= GUARD_MAX_VALUE:
            query = query.eq("content_value", str(current))
    try:
        res = query.execute()
    except Exception as exc:
        raise ContentStoreError(f"Enregistrement de '{row.get('xpath')}' impossible : {exc}") from exc
    return [item for item in (res.data or []) if isinstance(item, dict)]


def _stored_value(tenant_id: str, row_id: Any) -> str:
    try:
        res = (supabase.table(TABLE_CONTENT).select("content_value")
               .eq("tenant_id", tenant_id).eq("id", row_id).limit(1).execute())
    except Exception as exc:
        raise ContentStoreError(f"Relecture du contenu impossible : {exc}") from exc
    rows = res.data or []
    value = rows[0].get("content_value") if rows and isinstance(rows[0], dict) else None
    return "" if value is None else str(value)


def _change_outcome(change: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[dict[str, Any], Optional[str]]:
    """Décision pour une modification : ``(résultat, valeur à écrire ou None)``.

    « Inchangé » est testé avant « conflit » : renvoyer un lot déjà enregistré (réponse
    perdue, double clic) n'est jamais un conflit.
    """
    result: dict[str, Any] = {"xpath": change.get("xpath"), "file_path": change.get("file_path")}
    if not rows:
        return {**result, "status": "not_found"}, None
    value, error = content_sync.validate_value(rows[0].get("content_type") or "text", change.get("new_value"))
    if error:
        return {**result, "status": "invalid", "detail": error}, None
    current = ["" if row.get("content_value") is None else str(row["content_value"]) for row in rows]
    if all(item == value for item in current):
        return {**result, "status": "unchanged", "row": rows[0]}, None
    expected = change.get("expected_value")
    if expected is not None and any(item != expected for item in current):
        return {**result, "status": "conflict", "current_value": current[0]}, None
    return result, value


def apply_content_plan(tenant_id: str, file_path: str, plan: Any, anchors: bool) -> None:
    """Exécute un plan de ``content_sync.reconcile`` pour **une** page d'un tenant.

    Ordre imposé par l'unicité (tenant, page, xpath) : les clés à libérer
    (suppressions, détachements) d'abord, puis les déplacements en deux temps
    (une permutation de deux paragraphes ne se heurte jamais), puis les ancres,
    enfin les nouveaux emplacements — sans jamais écraser une ligne existante.

    Chaque écriture vise la ligne lue (identifiant) **et** la clé lue : si une autre
    réconciliation (requête renvoyée après un délai dépassé) l'a déjà déplacée, elle
    est sans effet au lieu de détacher ou déplacer une autre ligne.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ids = getattr(plan, "ids", {}) or {}

    def keyed(query: Any, key: str, row_id: Any = None) -> Any:
        query = query.eq("tenant_id", tenant_id).eq("file_path", file_path).eq("xpath", key)
        row_id = ids.get(key) if row_id is None else row_id
        return query.eq("id", row_id) if row_id is not None else query

    def table() -> Any:
        return supabase.table(TABLE_CONTENT)

    try:
        for key in plan.delete:
            keyed(table().delete(), key).execute()
        for key in plan.detach:
            keyed(table().update({"xpath": content_sync.orphan_key(key, stamp)}), key).execute()
        transit = []
        for index, (old_key, new_key) in enumerate(plan.moves):
            temporary = f"{content_sync.MOVING_PREFIX}{stamp}-{index}:{old_key}"
            keyed(table().update({"xpath": temporary}), old_key).execute()
            transit.append((temporary, new_key, ids.get(old_key)))
        for temporary, new_key, row_id in transit:
            keyed(table().update({"xpath": new_key}), temporary, row_id).execute()
        for key, source, value in plan.reanchors:
            fields: dict[str, Any] = {CONTENT_ANCHOR_COLUMN: source}
            if value is not None:
                fields["content_value"] = value
            keyed(table().update(fields), key).execute()
        for key, source in plan.backfills:
            keyed(table().update({CONTENT_ANCHOR_COLUMN: source}), key).is_(CONTENT_ANCHOR_COLUMN, "null").execute()
        rows = [
            {
                "tenant_id": tenant_id, "file_path": file_path, "xpath": slot.key,
                "content_type": slot.kind, "content_value": slot.source,
                **({CONTENT_ANCHOR_COLUMN: slot.source} if anchors else {}),
            }
            for slot in plan.inserts
        ]
        for start in range(0, len(rows), CONTENT_WRITE_CHUNK):
            (table().upsert(rows[start:start + CONTENT_WRITE_CHUNK], on_conflict=CONTENT_CONFLICT_KEY,
                            ignore_duplicates=True).execute())
    except Exception as exc:
        raise ContentStoreError(f"Réconciliation de '{tenant_id}/{file_path}' impossible : {exc}") from exc


def delete_detached_content(tenant_id: str, file_path: str, xpath: str) -> bool:
    """Supprime un contenu **détaché** (orphelin) ; un emplacement actif n'est jamais supprimé."""
    if not content_sync.is_detached(xpath):
        return False
    try:
        res = (supabase.table(TABLE_CONTENT).delete()
               .eq("tenant_id", tenant_id).eq("file_path", file_path).eq("xpath", xpath).execute())
    except Exception as exc:
        raise ContentStoreError(f"Suppression de '{xpath}' impossible : {exc}") from exc
    return bool(res.data)


def upsert_tenant_content(tenant_id: str, records: list[dict[str, Any]]) -> int:
    """Indexe des emplacements **nouveaux** pour un seul tenant (protocole v1 des conteneurs).

    Le ``tenant_id`` est imposé côté serveur : un appelant ne peut pas écrire dans
    les données d'un autre locataire, même en forgeant ses enregistrements. Une ligne
    existante n'est jamais écrasée : un conteneur ne peut pas effacer le travail du
    client en renvoyant les textes du gabarit. Renvoie le nombre de lignes créées ;
    lève ``ContentStoreError`` si la base refuse l'écriture.
    """
    clean = [r for r in (records or []) if isinstance(r, dict)]
    written = 0
    for start in range(0, len(clean), CONTENT_WRITE_CHUNK):
        chunk = [
            {
                "tenant_id": tenant_id,          # imposé, jamais repris du client
                "file_path": r.get("file_path"),
                "xpath": r.get("xpath"),
                "content_type": r.get("content_type") or "text",
                "content_value": r.get("content_value"),
            }
            for r in clean[start:start + CONTENT_WRITE_CHUNK]
        ]
        try:
            res = (supabase.table(TABLE_CONTENT)
                   .upsert(chunk, on_conflict=CONTENT_CONFLICT_KEY, ignore_duplicates=True).execute())
        except Exception as exc:
            raise ContentStoreError(f"Indexation du contenu de '{tenant_id}' impossible : {exc}") from exc
        written += len(res.data or [])
    return written


def delete_tenant_content(tenant_id: str) -> int:
    """Supprime l'ensemble des contenus associés à un tenant."""
    try:
        res = (
            supabase.table(TABLE_CONTENT)
            .delete()
            .eq("tenant_id", tenant_id)
            .execute()
        )
        return len(res.data or [])
    except Exception as e:
        print(f"[DB Error] delete_tenant_content: {e}")
        return 0


# ---------------------------------------------------------
# 2.1 ASSETS & MÉDIAS (SUPABASE STORAGE)
# ---------------------------------------------------------

def upload_tenant_image(tenant_id: str, file_bytes: bytes, file_name: str, content_type: str) -> Optional[str]:
    """Téléverse une image dans Supabase Storage et retourne son URL publique CDN."""
    bucket_name = "waas-media"

    # Nettoyage sommaire du nom de fichier et préfixe temporel
    clean_name = "".join(c for c in file_name if c.isalnum() or c in "._-").strip()
    storage_path = f"{tenant_id}/{int(time.time())}_{clean_name}"

    try:
        # Upload binaire avec Content-Type explicite
        supabase.storage.from_(bucket_name).upload(
            path=storage_path,
            file=file_bytes,
            file_options={"content-type": content_type, "upsert": "true"}
        )

        # Récupération de l'URL publique
        public_url = supabase.storage.from_(bucket_name).get_public_url(storage_path)
        return public_url
    except Exception as e:
        print(f"[DB Error] upload_tenant_image: {e}")
        return None


# ---------------------------------------------------------
# 3. SETTINGS DYNAMIQUES (FLEET_SETTINGS)
# ---------------------------------------------------------

_SETTINGS_CACHE: dict[str, str] = {}
_LAST_CACHE_FETCH = 0.0
CACHE_TTL_SECONDS = 60.0


def reload_settings_cache() -> None:
    """Recharge la table de configuration en mémoire (réaffectation atomique)."""
    global _SETTINGS_CACHE, _LAST_CACHE_FETCH
    try:
        res = supabase.table(TABLE_SETTINGS).select("key, value").execute()
    except Exception as e:
        print(f"[DB Warn] reload_settings_cache: {e}")
        return

    # Même sur réponse vide : évite une tempête de requêtes à chaque appel.
    _LAST_CACHE_FETCH = time.time()
    if res.data:
        _SETTINGS_CACHE = {
            str(row["key"]): row.get("value")
            for row in res.data
            if isinstance(row, dict) and row.get("key") is not None
        }


def get_settings_snapshot() -> dict[str, str]:
    """Instantané immuable du cache (sûr pour une sérialisation JSON concurrente)."""
    return dict(_SETTINGS_CACHE)


def get_setting(key: str, default: Optional[str] = None) -> str:
    """Récupère un paramètre dynamique avec cache mémoire."""
    global _LAST_CACHE_FETCH
    if not _SETTINGS_CACHE or (time.time() - _LAST_CACHE_FETCH) > CACHE_TTL_SECONDS:
        reload_settings_cache()
    return _SETTINGS_CACHE.get(key, default or "")


def set_setting(key: str, value: str) -> None:
    """Met à jour un paramètre en DB et rafraîchit le cache local (copie-sur-écriture)."""
    global _SETTINGS_CACHE
    try:
        supabase.table(TABLE_SETTINGS).upsert({"key": key, "value": value}).execute()
        # Jamais de mutation en place : un lecteur concurrent ne doit pas voir
        # "dictionary changed size during iteration".
        _SETTINGS_CACHE = {**_SETTINGS_CACHE, key: value}
    except Exception as e:
        print(f"[DB Error] set_setting: {e}")


# ---------------------------------------------------------
# 4. ANALYTICS (TENANT_ANALYTICS)
# ---------------------------------------------------------

def record_hit(tenant_id: str, visitor_id: str, path: str) -> None:
    """Insère un hit de visite anonymisé (zéro cookie)."""
    try:
        supabase.table(TABLE_ANALYTICS).insert({
            "tenant_id": tenant_id,
            "visitor_id": visitor_id,
            "path": path or "/",
        }).execute()
    except Exception as e:
        print(f"[DB Error] record_hit: {e}")


def get_tenant_analytics(tenant_id: str) -> dict[str, int]:
    """Récupère le nombre total de pages vues et de visiteurs uniques."""
    try:
        res = supabase.rpc("get_tenant_analytics_summary", {
            "p_tenant_id": tenant_id,
        }).execute()

        if res.data and len(res.data) > 0:
            row = res.data[0]
            return {
                "total_page_views": int(row.get("total_page_views", 0)),
                "unique_visitors": int(row.get("unique_visitors", 0)),
            }
    except Exception as e:
        print(f"[DB Error] get_tenant_analytics: {e}")

    return {"total_page_views": 0, "unique_visitors": 0}


# ---------------------------------------------------------
# 5. SONDES DE SANTÉ (TENANT_MONITORS)
# ---------------------------------------------------------

def get_tenant_monitors(tenant_id: str) -> list[dict[str, Any]]:
    """Récupère l'ensemble des sondes actives configurées pour un tenant."""
    try:
        res = (
            supabase.table(TABLE_MONITORS)
            .select("*")
            .eq("tenant_id", tenant_id)
            .eq("is_active", True)
            .execute()
        )
        return res.data or []
    except Exception as e:
        print(f"[DB Error] get_tenant_monitors: {e}")
        return []


def get_all_active_monitors() -> list[dict[str, Any]]:
    """Récupère l'ensemble des sondes actives de toute la flotte."""
    try:
        res = (
            supabase.table(TABLE_MONITORS)
            .select("*")
            .eq("is_active", True)
            .execute()
        )
        return res.data or []
    except Exception as e:
        print(f"[DB Error] get_all_active_monitors: {e}")
        return []


def create_tenant_monitor(data: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Enregistre une nouvelle sonde externe (HTTP/TCP)."""
    try:
        res = supabase.table(TABLE_MONITORS).insert(data).execute()
        if res.data and len(res.data) > 0:
            return res.data[0]
    except Exception as e:
        print(f"[DB Error] create_tenant_monitor: {e}")
    return None


def delete_tenant_monitor(monitor_id: str, tenant_id: Optional[str] = None) -> bool:
    """Supprime une sonde avec vérification optionnelle de tenant_id pour l'isolation client."""
    try:
        query = supabase.table(TABLE_MONITORS).delete().eq("id", monitor_id)
        if tenant_id:
            query = query.eq("tenant_id", tenant_id)
        res = query.execute()
        return bool(res.data and len(res.data) > 0)
    except Exception as e:
        print(f"[DB Error] delete_tenant_monitor: {e}")
        return False


def list_tenant_monitors(tenant_id: str) -> list[dict[str, Any]]:
    """Toutes les sondes d'un tenant, actives ou non (écran d'administration)."""
    return _read(
        TABLE_MONITORS,
        supabase.table(TABLE_MONITORS).select("*").eq("tenant_id", tenant_id).order("created_at"),
        [],
    )


def delete_tenant_monitors(tenant_id: str) -> int:
    """Purge des sondes d'un site supprimé."""
    try:
        return len(_write(TABLE_MONITORS, supabase.table(TABLE_MONITORS).delete().eq("tenant_id", tenant_id)))
    except Exception as exc:
        logger.warning("Purge des sondes de %s impossible : %s", tenant_id, exc)
        return 0


# ---------------------------------------------------------
# 6. TRAFIC BRUT (LECTURE INCRÉMENTALE POUR LA TÉLÉMÉTRIE)
# ---------------------------------------------------------

def fetch_hits_after(last_id: int, since_iso: Optional[str], limit: int = 1000) -> list[dict[str, Any]]:
    """Hits d'identifiant > ``last_id`` (ordre croissant), bornés dans le temps.

    L'identifiant ``bigint`` est monotone : le moteur de télémétrie n'a qu'à lire
    les nouvelles lignes à chaque cycle au lieu de rebalayer 14 jours de trafic.
    """
    query = (
        supabase.table(TABLE_ANALYTICS)
        .select("id, tenant_id, visitor_id, path, created_at")
        .gt("id", int(last_id))
        .order("id")
        .limit(max(1, min(int(limit), 5000)))
    )
    if since_iso:
        query = query.gte("created_at", since_iso)
    try:
        res = query.execute()
        return [row for row in (res.data or []) if isinstance(row, dict)]
    except Exception as exc:
        logger.warning("fetch_hits_after impossible : %s", exc)
        return []


def purge_tenant_analytics(tenant_id: str) -> int:
    try:
        res = supabase.table(TABLE_ANALYTICS).delete().eq("tenant_id", tenant_id).execute()
        return len(res.data or [])
    except Exception as exc:
        logger.warning("Purge du trafic de %s impossible : %s", tenant_id, exc)
        return 0


# ---------------------------------------------------------
# 7. FACTURATION (TABLE `invoices`)
# ---------------------------------------------------------

INVOICE_STATUSES = ("draft", "pending", "paid", "overdue")
INVOICE_FIELDS = (
    "site_id", "client_id", "client_name", "description", "amount", "currency", "status",
    "issued_at", "due_at", "paid_at", "items",
)


def list_invoices(site_id: Optional[str] = None, site_ids: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
    query = supabase.table(TABLE_INVOICES).select("*").order("issued_at", desc=True).order("number", desc=True)
    if site_id:
        query = query.eq("site_id", site_id)
    elif site_ids is not None:
        ids = [value for value in site_ids if value]
        if not ids:
            return []
        query = query.in_("site_id", ids)
    return _read(TABLE_INVOICES, query, [])


def subscription_invoiced_sites(period: str, since_day: str) -> set[str]:
    """Sites déjà facturés pour l'abonnement ``period`` (AAAA-MM), d'après le marqueur de ligne.

    Lecture **stricte** et paginée : une erreur ou une réponse tronquée ne doit jamais
    faire croire qu'aucune facture n'existe (sinon double facturation).
    """
    if not relation_ready(TABLE_INVOICES):
        raise SchemaNotReady(TABLE_INVOICES)
    sites: set[str] = set()
    start = 0
    while True:
        query = (
            supabase.table(TABLE_INVOICES).select("id, site_id, items").gte("issued_at", since_day)
            .order("id").range(start, start + PAGE_ROWS - 1)
        )
        page = _read(TABLE_INVOICES, query, [], strict=True)
        for row in page:
            items = row.get("items") if isinstance(row, dict) else None
            if any(isinstance(item, dict) and item.get("kind") == "subscription" and item.get("period") == period
                   for item in (items or [])):
                sites.add(str(row.get("site_id")))
        if len(page) < PAGE_ROWS:
            return sites
        start += PAGE_ROWS


def get_invoice(invoice_id: str) -> Optional[dict[str, Any]]:
    rows = _read(TABLE_INVOICES, supabase.table(TABLE_INVOICES).select("*").eq("id", invoice_id).limit(1), [])
    return rows[0] if rows else None


def _next_invoice_number(year: int) -> str:
    prefix = f"FAC-{year}-"
    rows = _read(
        TABLE_INVOICES,
        supabase.table(TABLE_INVOICES).select("number").like("number", f"{prefix}%")
        .order("number", desc=True).limit(1),
        [],
    )
    last = 0
    if rows:
        try:
            last = int(str(rows[0].get("number", "")).rsplit("-", 1)[-1])
        except ValueError:
            last = 0
    return f"{prefix}{last + 1:04d}"


def create_invoice(fields: dict[str, Any]) -> dict[str, Any]:
    """Crée une facture numérotée ``FAC-AAAA-NNNN`` (numéro réattribué en cas de collision)."""
    payload = {key: value for key, value in fields.items() if key in INVOICE_FIELDS}
    issued = str(payload.get("issued_at") or date.today().isoformat())
    year = int(issued[:4]) if issued[:4].isdigit() else date.today().year
    last_error: Optional[Exception] = None
    for _ in range(3):
        payload["number"] = _next_invoice_number(year)
        try:
            rows = _write(TABLE_INVOICES, supabase.table(TABLE_INVOICES).insert(payload))
        except DuplicateRecord as exc:
            last_error = exc
            continue
        if rows:
            return rows[0]
    raise RuntimeError(f"Numérotation de facture impossible : {last_error}")


def update_invoice(invoice_id: str, fields: dict[str, Any]) -> Optional[dict[str, Any]]:
    payload = {key: value for key, value in fields.items() if key in INVOICE_FIELDS}
    rows = _write(TABLE_INVOICES, supabase.table(TABLE_INVOICES).update(payload).eq("id", invoice_id))
    return rows[0] if rows else None


def delete_invoice(invoice_id: str) -> bool:
    return bool(_write(TABLE_INVOICES, supabase.table(TABLE_INVOICES).delete().eq("id", invoice_id)))


# ---------------------------------------------------------
# 8. SUPPORT (TABLES `tickets` & `ticket_messages`)
# ---------------------------------------------------------

TICKET_STATUSES = ("open", "waiting", "closed")
TICKET_PRIORITIES = ("low", "normal", "urgent")
_TICKET_SELECT = "*, ticket_messages(*)"


def _shape_ticket(row: dict[str, Any]) -> dict[str, Any]:
    messages = [m for m in (row.pop("ticket_messages", None) or []) if isinstance(m, dict)]
    messages.sort(key=lambda m: str(m.get("sent_at") or ""))
    return {**row, "messages": messages}


def list_tickets(site_id: Optional[str] = None, site_ids: Optional[Iterable[str]] = None,
                 include_closed: bool = True) -> list[dict[str, Any]]:
    query = supabase.table(TABLE_TICKETS).select(_TICKET_SELECT).order("updated_at", desc=True)
    if site_id:
        query = query.eq("site_id", site_id)
    elif site_ids is not None:
        ids = [value for value in site_ids if value]
        if not ids:
            return []
        query = query.in_("site_id", ids)
    if not include_closed:
        query = query.neq("status", "closed")
    return [_shape_ticket(row) for row in _read(TABLE_TICKETS, query, []) if isinstance(row, dict)]


def get_ticket(ticket_id: str) -> Optional[dict[str, Any]]:
    rows = _read(TABLE_TICKETS, supabase.table(TABLE_TICKETS).select(_TICKET_SELECT).eq("id", ticket_id).limit(1), [])
    return _shape_ticket(rows[0]) if rows and isinstance(rows[0], dict) else None


def create_ticket(site_id: str, subject: str, body: str, author_name: str,
                  from_role: str = "client", priority: str = "normal") -> dict[str, Any]:
    rows = _write(TABLE_TICKETS, supabase.table(TABLE_TICKETS).insert({
        "site_id": site_id,
        "subject": subject,
        "priority": priority if priority in TICKET_PRIORITIES else "normal",
        "status": "open" if from_role == "client" else "waiting",
    }))
    if not rows:
        raise RuntimeError("Création du ticket refusée par la base.")
    ticket = rows[0]
    try:
        add_ticket_message(str(ticket["id"]), body, author_name, from_role, touch=False)
    except Exception:
        # Pas de conversation vide orpheline : le ticket n'existe qu'avec son premier message.
        with_suppressed_errors(lambda: supabase.table(TABLE_TICKETS).delete().eq("id", ticket["id"]).execute())
        raise
    return get_ticket(str(ticket["id"])) or {**ticket, "messages": []}


def with_suppressed_errors(action: Any) -> None:
    try:
        action()
    except Exception as exc:
        logger.warning("Nettoyage impossible : %s", exc)


def add_ticket_message(ticket_id: str, body: str, author_name: str, from_role: str,
                       touch: bool = True) -> dict[str, Any]:
    """Ajoute un message ; une réponse de l'agence met le ticket « en attente » du client."""
    rows = _write(TABLE_TICKET_MESSAGES, supabase.table(TABLE_TICKET_MESSAGES).insert({
        "ticket_id": ticket_id,
        "from_role": from_role,
        "author_name": author_name,
        "body": body,
    }))
    if touch:
        _write(TABLE_TICKETS, supabase.table(TABLE_TICKETS).update({
            "status": "waiting" if from_role == "agency" else "open",
            "updated_at": _now_iso(),
        }).eq("id", ticket_id))
    return rows[0] if rows else {}


def set_ticket_status(ticket_id: str, status: str) -> Optional[dict[str, Any]]:
    if status not in TICKET_STATUSES:
        raise ValueError(f"Statut de ticket invalide : {status}")
    rows = _write(TABLE_TICKETS, supabase.table(TABLE_TICKETS).update({
        "status": status, "updated_at": _now_iso(),
    }).eq("id", ticket_id))
    return rows[0] if rows else None


def mark_ticket_seen(ticket_id: str, reader_role: str) -> None:
    """Marque comme lus les messages de l'autre partie (meilleur effort)."""
    other = "client" if reader_role == "agency" else "agency"
    try:
        supabase.table(TABLE_TICKET_MESSAGES).update({"seen_at": _now_iso()}) \
            .eq("ticket_id", ticket_id).eq("from_role", other).is_("seen_at", "null").execute()
    except Exception as exc:
        logger.debug("Accusé de lecture non enregistré (%s) : %s", ticket_id, exc)


def count_open_tickets() -> int:
    rows = _read(TABLE_TICKETS, supabase.table(TABLE_TICKETS).select("id").neq("status", "closed"), [])
    return len(rows)


# ---------------------------------------------------------
# 9. SUPERVISION : DISPONIBILITÉ QUOTIDIENNE & CHARGE HORAIRE
# ---------------------------------------------------------

def upsert_uptime_rows(rows: list[dict[str, Any]]) -> bool:
    """Enregistre les agrégats quotidiens ; ``False`` si la table n'existe pas encore."""
    if not rows:
        return True
    try:
        _write(TABLE_UPTIME, supabase.table(TABLE_UPTIME).upsert(rows, on_conflict="service_key,day"))
        return True
    except SchemaNotReady:
        return False


#: PostgREST plafonne chaque réponse (``max-rows``, 1000 par défaut sur Supabase) :
#: au-delà, un ``.limit()`` plus grand est silencieusement tronqué — on pagine.
PAGE_ROWS = 1000
MAX_UPTIME_ROWS = 50_000


def _read_pages(relation: str, build_query: Any, max_rows: int) -> list[dict[str, Any]]:
    """Lecture paginée par ``.range()`` (requête reconstruite à chaque page, ordre stable)."""
    rows: list[dict[str, Any]] = []
    while len(rows) < max_rows:
        start = len(rows)
        page = _read(relation, build_query().range(start, start + PAGE_ROWS - 1), [])
        rows.extend(row for row in page if isinstance(row, dict))
        if len(page) < PAGE_ROWS:
            break
    return rows[:max_rows]


def get_uptime_rows(since_day: str, prefix: Optional[str] = None) -> list[dict[str, Any]]:
    def build() -> Any:
        query = supabase.table(TABLE_UPTIME).select("*").gte("day", since_day)
        if prefix:
            query = query.like("service_key", f"{prefix}%")
        return query.order("day").order("service_key")

    return _read_pages(TABLE_UPTIME, build, MAX_UPTIME_ROWS)


def delete_uptime_rows(service_key: str) -> int:
    try:
        return len(_write(TABLE_UPTIME, supabase.table(TABLE_UPTIME).delete().eq("service_key", service_key)))
    except SchemaNotReady:
        return 0


def upsert_host_hourly(rows: list[dict[str, Any]]) -> bool:
    if not rows:
        return True
    try:
        _write(TABLE_HOST_HOURLY, supabase.table(TABLE_HOST_HOURLY).upsert(rows, on_conflict="hour"))
        return True
    except SchemaNotReady:
        return False


def get_host_hourly(since_iso: str) -> list[dict[str, Any]]:
    return _read(
        TABLE_HOST_HOURLY,
        supabase.table(TABLE_HOST_HOURLY).select("*").gte("hour", since_iso).order("hour").limit(1000),
        [],
    )