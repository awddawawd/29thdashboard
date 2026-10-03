"""Moteur de synchronisation du conteneur locataire (v2).

Ce processus s'exécute **dans le conteneur du client**, c'est-à-dire dans un
environnement non fiable. Il ne détient donc aucun secret du plan de contrôle :

* aucune clé Supabase : un simple jeton HMAC ``SYNC_TOKEN``, scopé à
  ``TENANT_ID``, transmis à l'API WaaS qui applique le filtrage locataire ;
* uniquement la bibliothèque standard pour les appels HTTP (aucune dépendance
  supplémentaire dans l'image, surface d'attaque réduite).

Cycle (au démarrage, puis à chaque publication à chaud) :

1. le gabarit cloné est copié tel quel dans ``WAAS_PRISTINE_DIR`` (tmpfs) : il
   reste la référence de toute hydratation, qui n'opère jamais sur une page déjà
   hydratée ;
2. chaque page est analysée (``waas_content``) et son inventaire d'emplacements
   envoyé à l'API, qui indexe les nouveaux éléments, suit ceux que le gabarit a
   déplacés et renvoie les valeurs du client (protocole v2, repli v1) ;
3. la page publique est réécrite atomiquement, avec sa carte d'édition et le pont
   du Studio (chargé uniquement dans l'aperçu encadré).

``python3 sync_engine.py --rehydrate`` rejoue les étapes 2 et 3 sans
redémarrage : c'est la publication à chaud déclenchée par l'API (``docker exec``).
"""

import hashlib
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional
import urllib.error
import urllib.request
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import waas_content  # noqa: E402  (module voisin, copié dans /opt/scripts)

# ---------------------------------------------------------
# VARIABLES D'ENVIRONNEMENT RUNTIME
# ---------------------------------------------------------

TENANT_ID = os.getenv("TENANT_ID")
SYNC_TOKEN = os.getenv("SYNC_TOKEN")
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
#: URL de l'API **vue du navigateur du visiteur**, utilisée pour le seul
#: micro-tracker. ``API_BASE_URL`` répond à « comment le conteneur joint l'API »
#: (souvent ``http://host.docker.internal:<port>``) : cet hôte ne résout pas
#: depuis le poste du visiteur, donc ``sendBeacon`` échouait en DNS — en
#: silence, sans requête visible dans l'onglet réseau. Repli sur
#: ``API_BASE_URL`` pour les images antérieures à ce réglage.
BROWSER_API_URL = (os.getenv("BROWSER_API_URL") or "").strip() or API_BASE_URL
WEB_ROOT = os.getenv("WEB_ROOT", "/var/www/html")
#: Copie intacte du gabarit (tmpfs, effacée au redémarrage, qui reclone le dépôt).
PRISTINE_DIR = os.getenv("WAAS_PRISTINE_DIR") or os.path.join(tempfile.gettempdir(), "waas-pristine")
#: Sources du pont d'édition (dossier de parties *.js assemblées dans l'ordre des noms).
BRIDGE_SOURCE = os.getenv("WAAS_BRIDGE_SOURCE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "editor")
LOCK_PATH = os.path.join(tempfile.gettempdir(), "waas-sync.lock")

SYNC_ENDPOINT = "/internal/sync/content"
INVENTORY_ENDPOINT = "/internal/sync/inventory"
SYNC_TIMEOUT = float(os.getenv("SYNC_TIMEOUT", "30"))
#: Une réconciliation (première indexation, mise à niveau) peut écrire des centaines de
#: lignes : délai plus long, pour ne pas renvoyer une requête encore en cours de traitement.
INVENTORY_TIMEOUT = max(SYNC_TIMEOUT, float(os.getenv("INVENTORY_TIMEOUT", "120")))
SYNC_ATTEMPTS = int(os.getenv("SYNC_ATTEMPTS", "5"))
SYNC_PUSH_CHUNK = 500
MAX_INVENTORY_SLOTS = 5000
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
ASSETS_DIR = "__waas"
REHYDRATE_MARKER = "WAAS_REHYDRATE_OK"

if not TENANT_ID or not SYNC_TOKEN:
    print(
        "[SyncEngine - ERREUR] TENANT_ID ou SYNC_TOKEN manquant : ce conteneur n'a pas été "
        "provisionné par l'API WaaS. Recréez-le via /admin/api/sites/deploy."
    )
    sys.exit(1)


# ---------------------------------------------------------
# ACCÈS AU PLAN DE DONNÉES (API SCOPÉE PAR JETON)
# ---------------------------------------------------------

class EndpointMissing(RuntimeError):
    """L'API ne connaît pas cette route (version antérieure) : on négocie un repli."""


class RequestRejected(RuntimeError):
    """Requête refusée pour son contenu (413 / 422) : inutile de réessayer."""


def _raise_if_final(exc: urllib.error.HTTPError, method: str, path: str) -> None:
    """Erreurs HTTP définitives (aucun nouvel essai) ; les autres (5xx…) sont retentées."""
    detail = ""
    try:
        detail = exc.read(2048).decode("utf-8", "replace")
    except Exception:
        pass
    if exc.code in (404, 405):
        raise EndpointMissing(f"{method} {path} : route inconnue de l'API (HTTP {exc.code}).") from exc
    print(f"[SyncEngine - API] HTTP {exc.code} sur {path} : {detail}")
    if exc.code in (413, 422):
        raise RequestRejected(f"Requête refusée par l'API (HTTP {exc.code}) : {detail}") from exc
    if exc.code in (400, 401, 403):
        raise RuntimeError(f"Refus de l'API de synchronisation (HTTP {exc.code}).") from exc


def _api_call(
    method: str,
    path: str,
    payload: Any | None = None,
    timeout: float = SYNC_TIMEOUT,
    attempts: int = SYNC_ATTEMPTS,
) -> dict[str, Any]:
    """Appel authentifié à l'API WaaS, avec repli exponentiel borné.

    Lève si l'API est injoignable : mieux vaut redémarrer le conteneur que servir
    un site non hydraté (le contenu personnalisé du client serait absent).
    """
    url = f"{API_BASE_URL.rstrip('/')}{path}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("X-WAAS-Sync-Token", SYNC_TOKEN)
        request.add_header("Accept", "application/json")
        if body is not None:
            request.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES)
            return json.loads(raw.decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            _raise_if_final(exc, method, path)
            last_error = exc
        except Exception as exc:  # réseau, DNS, timeout, TLS…
            last_error = exc

        if attempt < attempts:
            delay = min(3 * 2 ** (attempt - 1), 30)
            print(
                f"[SyncEngine - API] {method} {path} tentative {attempt}/{attempts} échouée "
                f"({last_error}) — nouvel essai dans {delay}s"
            )
            time.sleep(delay)

    raise RuntimeError(f"API de synchronisation injoignable ({path}) : {last_error}")


def fetch_content() -> list[dict[str, Any]]:
    """Contenu déjà enregistré pour ce locataire (protocole v1 ; jeton = seul discriminant)."""
    response = _api_call("GET", SYNC_ENDPOINT)
    records = response.get("records") or []
    return [r for r in records if isinstance(r, dict)]


def push_content(records: list[dict[str, Any]]) -> int:
    """Indexe des éléments nouveaux (protocole v1), par lots bornés."""
    written = 0
    for start in range(0, len(records), SYNC_PUSH_CHUNK):
        chunk = records[start:start + SYNC_PUSH_CHUNK]
        response = _api_call("POST", SYNC_ENDPOINT, payload={"records": chunk})
        written += int(response.get("upserted") or 0)
    return written


class SyncClient:
    """Négocie le protocole : v2 (réconciliation par l'API), sinon v1 (API antérieure)."""

    def __init__(self) -> None:
        self.protocol: Optional[int] = None
        self._legacy_records: Optional[list[dict[str, Any]]] = None

    def values_for(self, page: waas_content.Page) -> dict[str, str]:
        if self.protocol != 1:
            slots = page.inventory()
            if len(slots) > MAX_INVENTORY_SLOTS:
                print(f"[SyncEngine] {page.rel_path} : {len(slots)} éléments, seuls les "
                      f"{MAX_INVENTORY_SLOTS} premiers sont éditables.")
                slots = slots[:MAX_INVENTORY_SLOTS]
            try:
                response = _api_call("POST", INVENTORY_ENDPOINT, payload={
                    "engine": waas_content.ENGINE_VERSION, "file_path": page.rel_path, "slots": slots,
                }, timeout=INVENTORY_TIMEOUT)
            except EndpointMissing:
                print("[SyncEngine] API antérieure au moteur v2 : protocole v1 (sans réconciliation).")
            else:
                if response.get("protocol") == 2:
                    self.protocol = 2
                    return {
                        item["xpath"]: item["content_value"]
                        for item in response.get("values") or []
                        if isinstance(item, dict) and isinstance(item.get("xpath"), str)
                        and isinstance(item.get("content_value"), str)
                    }
                print("[SyncEngine] Réponse d'inventaire inattendue : protocole v1.")
            self.protocol = 1
        return self._legacy_values(page)

    def _legacy_values(self, page: waas_content.Page) -> dict[str, str]:
        if self._legacy_records is None:
            self._legacy_records = fetch_content()
        known = {r.get("xpath"): r for r in self._legacy_records if r.get("file_path") == page.rel_path}
        missing = [
            {"file_path": page.rel_path, "xpath": slot.key, "content_type": slot.kind,
             "content_value": slot.source}
            for slot in page.slots if slot.key not in known
        ]
        if missing:
            push_content(missing)
        values = {}
        for key, record in known.items():
            slot = page.by_key.get(key)
            value = record.get("content_value")
            if slot is not None and isinstance(value, str) and not waas_content.slot_matches(slot, value):
                values[key] = value
        return values


# ---------------------------------------------------------
# MICRO-TRACKER SANS COOKIE
# ---------------------------------------------------------

# URL d'analytics sérialisée en JSON : une valeur de réglage malformée
# (`browser_api_url`) ne doit jamais pouvoir casser la chaîne JS ni injecter du
# script dans toutes les vitrines (XSS stockée de flotte). Elle doit être
# joignable **depuis le navigateur du visiteur** (voir BROWSER_API_URL).
_ANALYTICS_ENDPOINT = (
    f"{BROWSER_API_URL.rstrip('/')}/waas/analytics/hit/{quote(str(TENANT_ID), safe='')}"
)
TRACKER_SNIPPET = waas_content.tracker_snippet(_ANALYTICS_ENDPOINT)


# ---------------------------------------------------------
# FICHIERS DU SITE
# ---------------------------------------------------------

def collect_html_files(root_dir: str) -> list[tuple[str, str]]:
    """Pages HTML ``(chemin absolu, chemin relatif)`` : ni dossiers cachés, ni liens
    symboliques (un lien ne doit jamais faire lire un fichier hors du site)."""
    html_files: list[tuple[str, str]] = []
    for root, dirs, files in os.walk(root_dir):
        dirs[:] = sorted(
            name for name in dirs
            if not name.startswith(".") and name != ASSETS_DIR and not os.path.islink(os.path.join(root, name))
        )
        for name in sorted(files):
            abs_path = os.path.join(root, name)
            if name.lower().endswith((".html", ".htm")) and not os.path.islink(abs_path):
                rel_path = os.path.relpath(abs_path, root_dir).replace("\\", "/")
                html_files.append((abs_path, rel_path))
    return html_files


def write_atomic(path: str, data: bytes) -> None:
    """Écriture atomique : Nginx ne sert jamais une page à moitié écrite.

    Le fichier temporaire commence par un point : Nginx refuse de le servir.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temporary = os.path.join(directory, f".waas-{os.getpid()}-{os.path.basename(path)}.tmp")
    with open(temporary, "wb") as handle:
        handle.write(data)
    os.replace(temporary, path)


def _is_hydrated(data: bytes) -> bool:
    return waas_content.LOADER_MARKER.encode("ascii") in data


def snapshot_pristine() -> int:
    """Copie le gabarit tout juste cloné : seule référence des hydratations suivantes.

    Une page déjà hydratée (moteur relancé sans reclonage) ne remplace jamais sa
    copie d'origine : elle contient le contenu du client, pas celui du gabarit.
    """
    files = collect_html_files(WEB_ROOT)
    kept = set()
    for abs_path, rel_path in files:
        target = os.path.join(PRISTINE_DIR, rel_path)
        kept.add(os.path.normcase(os.path.abspath(target)))
        with open(abs_path, "rb") as handle:
            data = handle.read()
        if _is_hydrated(data) and os.path.isfile(target):
            continue
        if _is_hydrated(data):
            print(f"[SyncEngine] {rel_path} : page déjà hydratée sans copie d'origine, reprise telle quelle.")
        write_atomic(target, data)
    for abs_path, _rel_path in collect_html_files(PRISTINE_DIR):
        if os.path.normcase(os.path.abspath(abs_path)) not in kept:
            os.remove(abs_path)
    return len(files)


BRIDGE_PREAMBLE = """/*! Pont d'édition du Studio — WaaS · moteur v2 · 29th studio © 2026
 * Assemblé au démarrage du conteneur depuis /opt/scripts/editor (une seule portée).
 * Chargé uniquement dans l'aperçu encadré du Studio (?waas_edit=true).
 */
(function () {
'use strict';
if (window.__waasEditorBridge) return;
Object.defineProperty(window, '__waasEditorBridge', { value: true });
"""


def bundle_bridge(source: str) -> bytes:
    """Assemble les parties du pont (dossier, ordre des noms) en un seul script servi."""
    if not os.path.isdir(source):
        with open(source, "rb") as handle:
            return handle.read()
    parts = sorted(name for name in os.listdir(source) if name.endswith(".js"))
    if not parts:
        raise OSError(f"aucune partie *.js dans {source}")
    chunks = []
    for name in parts:
        with open(os.path.join(source, name), "r", encoding="utf-8") as handle:
            chunks.append(f"/* --- {name} --- */\n{handle.read()}")
    return (BRIDGE_PREAMBLE + "\n".join(chunks) + "\n})();\n").encode("utf-8")


def install_bridge_asset() -> Optional[str]:
    """Publie le pont d'édition du Studio ; renvoie son URL versionnée (cache navigateur)."""
    try:
        data = bundle_bridge(BRIDGE_SOURCE)
    except OSError as exc:
        print(f"[SyncEngine] Pont d'édition indisponible ({exc}) : édition visuelle désactivée.")
        return None
    write_atomic(os.path.join(WEB_ROOT, ASSETS_DIR, "editor-bridge.js"), data)
    return f"/{ASSETS_DIR}/editor-bridge.js?v={hashlib.sha256(data).hexdigest()[:12]}"


def sync_page(client: SyncClient, abs_path: str, rel_path: str, bridge_src: Optional[str]) -> int:
    """Hydrate une page depuis le gabarit ; renvoie le nombre de valeurs appliquées."""
    with open(abs_path, "rb") as handle:
        raw = handle.read()
    target = os.path.join(WEB_ROOT, rel_path)
    try:
        page = waas_content.Page.parse(raw, rel_path)
    except Exception as exc:  # HTML illisible : la page reste servie telle quelle
        print(f"[SyncEngine] {rel_path} : HTML illisible ({exc}), servi sans édition.")
        write_atomic(target, raw)
        return 0
    try:
        values = client.values_for(page)
    except RequestRejected as exc:
        # Refus déterministe : redémarrer ne changerait rien, la page est servie
        # avec le texte du gabarit plutôt que de faire boucler tout le site.
        print(f"[SyncEngine - ERREUR] {rel_path} : {exc} — page servie sans le contenu du client.")
        values = {}
    try:
        report = page.apply(values)
        rendered = page.render(tracker=TRACKER_SNIPPET, bridge_src=bridge_src)
    except Exception as exc:  # défaut propre à cette page : le reste du site est servi
        print(f"[SyncEngine - ERREUR] {rel_path} : hydratation impossible ({exc!r}), page servie telle quelle.")
        write_atomic(target, raw)
        return 0
    write_atomic(target, rendered.html)
    if rendered.map_json is not None:
        write_atomic(os.path.join(WEB_ROOT, ASSETS_DIR, "map", f"{rel_path}.json"),
                     rendered.map_json.encode("utf-8"))
    if report.unknown or report.rejected:
        print(f"[SyncEngine] {rel_path} : {len(report.unknown)} valeur(s) sans emplacement, "
              f"{len(report.rejected)} refusée(s).")
    return report.applied


@contextmanager
def _sync_lock() -> Iterator[None]:
    """Une seule synchronisation à la fois (démarrage et publications à chaud successives)."""
    try:
        import fcntl
    except ImportError:          # hors Linux (tests) : exécution non concurrente
        yield
        return
    with open(LOCK_PATH, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# ---------------------------------------------------------
# POINT D'ENTRÉE PRINCIPAL DU PROCESSUS
# ---------------------------------------------------------

def run_sync(rehydrate: bool = False) -> int:
    print(f"[SyncEngine] {'Réhydratation à chaud' if rehydrate else 'Démarrage du cycle'} "
          f"pour le tenant : {TENANT_ID}")
    print(f"[SyncEngine] Répertoire de travail : {WEB_ROOT}")
    print(f"[SyncEngine] API de synchronisation : {API_BASE_URL}{INVENTORY_ENDPOINT}")
    print(f"[SyncEngine] Micro-tracker injecté (appelé par le navigateur) : {_ANALYTICS_ENDPOINT}")

    with _sync_lock():
        if not rehydrate:
            snapshot_pristine()
        elif not os.path.isdir(PRISTINE_DIR):
            raise RuntimeError("Gabarit d'origine absent : un redémarrage est nécessaire.")
        bridge_src = install_bridge_asset()
        client = SyncClient()
        files = collect_html_files(PRISTINE_DIR)
        applied = sum(sync_page(client, abs_path, rel_path, bridge_src) for abs_path, rel_path in files)

    print(f"[SyncEngine] {len(files)} page(s) synchronisée(s), {applied} valeur(s) du client "
          f"appliquée(s) (protocole v{client.protocol or '-'}).")
    if rehydrate:
        print(f"{REHYDRATE_MARKER} files={len(files)} applied={applied}", flush=True)
    else:
        print("[SyncEngine] Prêt pour le démarrage de Nginx.")
    return applied


def main(argv: list[str]) -> int:
    try:
        run_sync(rehydrate="--rehydrate" in argv)
    except Exception as exc:
        print(f"[SyncEngine - ERREUR] {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
