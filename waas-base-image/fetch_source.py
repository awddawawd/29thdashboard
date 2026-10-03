"""Récupération du code source de la vitrine **via l'API WaaS** (``SOURCE_MODE=api``).

Ce script s'exécute dans le conteneur du client, environnement non fiable : il
n'y a ici **aucun identifiant Git**. L'API télécharge l'archive du dépôt (privé
ou non) avec le jeton GitHub de la plateforme et la remet à ce conteneur,
authentifié par son ``SYNC_TOKEN`` scopé et sa clé ``SOURCE_NONCE`` : le dépôt
servi est celui de ce conteneur, jamais un paramètre de la requête.

L'archive est extraite en flux avec le filtre ``data`` de ``tarfile`` (PEP 706) :
aucun fichier hors de la destination, aucun lien qui en sort, aucun fichier
spécial, volume et nombre d'éléments bornés. Le flux gzip est lu jusqu'à son
terme (CRC et longueur vérifiés) : une archive tronquée n'est jamais déployée.
Bibliothèque standard uniquement.

Usage : python3 fetch_source.py <dossier de destination>
"""

import gzip
import http.client
import os
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import Any, Optional

SOURCE_ENDPOINT = "/internal/sync/source"
SYNC_TOKEN_HEADER = "X-WAAS-Sync-Token"
SOURCE_NONCE_HEADER = "X-WAAS-Source-Nonce"
CHUNK_BYTES = 64 * 1024
SOURCE_TIMEOUT = float(os.getenv("SOURCE_TIMEOUT", "60"))
SOURCE_ATTEMPTS = int(os.getenv("SOURCE_ATTEMPTS", "5"))
#: Le contenu extrait vit dans /tmp (tmpfs de 128 Mo) avant sa copie dans la racine web.
MAX_EXTRACTED_BYTES = 96 * 1024 * 1024
MAX_MEMBERS = 20_000
#: Refus définitifs de l'API : réessayer ne changerait rien (jeton, dépôt, mode).
FATAL_STATUSES = frozenset({400, 401, 403, 404, 409, 413, 422})
#: Échecs transitoires : réseau, archive tronquée par une coupure.
RETRYABLE_ERRORS = (OSError, http.client.HTTPException, tarfile.TarError, EOFError, zlib.error)
#: Premières versions corrigeant les contournements du filtre « data »
#: (CVE-2025-4517, CVE-2025-4330, CVE-2025-4138…) ; 3.14 et suivantes le sont toutes.
SAFE_TARFILE_PATCH = {(3, 11): 13, (3, 12): 11, (3, 13): 4}


class FetchError(Exception):
    """Échec définitif : le conteneur s'arrête (Docker le relancera)."""


def log(message: str) -> None:
    print(f"[Source] {message}", flush=True)


class SafeMembers:
    """Filtre d'extraction : retire le dossier racine des archives GitHub
    (``<propriétaire>-<dépôt>-<sha>/``), applique le filtre ``data`` et borne le volume."""

    def __init__(self) -> None:
        self.members = 0
        self.size = 0

    def __call__(self, member: tarfile.TarInfo, destination: str) -> Optional[tarfile.TarInfo]:
        relative = member.name.partition("/")[2].strip("/")
        if not relative:
            return None
        self.members += 1
        self.size += max(member.size, 0)
        if self.members > MAX_MEMBERS or self.size > MAX_EXTRACTED_BYTES:
            raise FetchError(
                f"Dépôt trop volumineux (plus de {MAX_MEMBERS} éléments ou de "
                f"{MAX_EXTRACTED_BYTES // (1024 * 1024)} Mo une fois extrait)."
            )
        if member.islnk():
            log(f"Lien physique ignoré : {relative}")
            return None
        try:
            return tarfile.data_filter(member.replace(name=relative, deep=False), destination)
        except tarfile.FilterError as exc:
            log(f"Élément ignoré (sort du site ou fichier spécial) : {relative} ({type(exc).__name__})")
            return None


def tarfile_is_safe(version: tuple[int, ...] = tuple(sys.version_info[:3])) -> bool:
    """Filtres d'extraction présents **et** exempts des contournements connus."""
    if not hasattr(tarfile, "data_filter"):
        return False
    if tuple(version[:2]) >= (3, 14):
        return True
    minimum = SAFE_TARFILE_PATCH.get(tuple(version[:2]))
    return minimum is not None and version[2] >= minimum


def extract_archive(stream: Any, destination: Path) -> Optional[str]:
    """Extrait l'archive ``tar.gz`` lue en flux ; renvoie le commit si GitHub l'indique."""
    accept = SafeMembers()
    # GzipFile plutôt que « r|gz » : un flux interrompu lève EOFError même si la coupure
    # tombe entre deux fichiers, là où tarfile seul y verrait la fin de l'archive.
    with gzip.GzipFile(fileobj=stream, mode="rb") as compressed:
        with tarfile.open(fileobj=compressed, mode="r|") as archive:
            for member in archive:
                archive.extract(member, path=destination, filter=accept)
            commit = archive.pax_headers.get("comment")
        # Lecture jusqu'au terme du flux : CRC32 et longueur gzip vérifiés.
        while compressed.read(CHUNK_BYTES):
            pass
    if accept.members == 0:
        raise FetchError("Archive vide : le dépôt ne contient aucun fichier.")
    return commit


def _download(api_base: str, token: str, nonce: str, destination: Path) -> Optional[str]:
    request = urllib.request.Request(f"{api_base.rstrip('/')}{SOURCE_ENDPOINT}", method="GET")
    request.add_header(SYNC_TOKEN_HEADER, token)
    request.add_header(SOURCE_NONCE_HEADER, nonce)
    request.add_header("Accept", "application/gzip")
    with urllib.request.urlopen(request, timeout=SOURCE_TIMEOUT) as response:
        return extract_archive(response, destination)


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read(2048).decode("utf-8", "replace")
    except Exception:
        return ""


def fetch_source(destination: Path, api_base: str, token: str, nonce: str,
                 attempts: int = SOURCE_ATTEMPTS) -> Optional[str]:
    """Télécharge et extrait le dépôt dans ``destination`` (vidée à chaque essai)."""
    last_error = ""
    for attempt in range(1, attempts + 1):
        shutil.rmtree(destination, ignore_errors=True)
        destination.mkdir(parents=True)
        try:
            return _download(api_base, token, nonce, destination)
        except urllib.error.HTTPError as exc:
            detail = _error_detail(exc)
            if exc.code in FATAL_STATUSES:
                raise FetchError(f"refus de l'API (HTTP {exc.code}) : {detail}") from None
            last_error = f"HTTP {exc.code} : {detail}"
        except RETRYABLE_ERRORS as exc:     # FetchError (définitif) n'en fait pas partie
            last_error = f"{type(exc).__name__} : {exc}"
        if attempt < attempts:
            delay = min(3 * 2 ** (attempt - 1), 30)
            log(f"Tentative {attempt}/{attempts} échouée ({last_error}) — nouvel essai dans {delay}s")
            time.sleep(delay)
    raise FetchError(f"archive indisponible après {attempts} tentatives ({last_error})")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        log("Usage : fetch_source.py <dossier de destination>")
        return 2
    token = os.getenv("SYNC_TOKEN") or ""
    nonce = os.getenv("SOURCE_NONCE") or ""
    api_base = os.getenv("API_BASE_URL") or ""
    if not token or not nonce or not api_base:
        log("ERREUR : SYNC_TOKEN, SOURCE_NONCE ou API_BASE_URL manquant "
            "(conteneur non provisionné par l'API WaaS : recréez-le).")
        return 1
    if not tarfile_is_safe():
        log(f"ERREUR : Python {sys.version.split()[0]} sans extraction tar sûre (CVE-2025-4517) : "
            "reconstruisez l'image sur une version corrigée.")
        return 1
    try:
        commit = fetch_source(Path(argv[1]), api_base, token, nonce)
    except FetchError as exc:
        log(f"ERREUR : {exc}")
        return 1
    log(f"Commit déployé : {commit or 'inconnu'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
