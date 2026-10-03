"""Source des vitrines : dépôts GitHub **privés** sans identifiant dans le conteneur.

Un conteneur locataire s'exécute dans un environnement non fiable : il ne doit
détenir aucun secret du plan de contrôle. Un jeton Git dans ``REPO_URL``
finissait d'ailleurs dans ``.git/config``, servi publiquement. Pour un dépôt
privé, c'est donc l'API qui télécharge l'archive avec le jeton de la plateforme
(``WAAS_GITHUB_TOKEN``) et la remet au conteneur, authentifié par son
``SYNC_TOKEN`` scopé et sa clé ``SOURCE_NONCE``. Le jeton GitHub ne quitte
jamais le processus API.

Modes de récupération transmis au conteneur (``SOURCE_MODE``) :

* ``api`` : dépôt github.com **et** jeton défini (public ou privé) ;
* ``git`` : tout le reste, ``git clone`` public dans le conteneur (inchangé).
"""

from __future__ import annotations

from contextlib import contextmanager
from enum import Enum
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from typing import BinaryIO, Iterator, NamedTuple, Optional
from urllib.parse import quote, urljoin, urlparse

import httpx

logger = logging.getLogger("waas.git_source")

GITHUB_HOST = "github.com"
GITHUB_API = "https://api.github.com"
GITHUB_TOKEN_ENV = "WAAS_GITHUB_TOKEN"
#: Seuls hôtes joints pour une archive : l'API (avec jeton) puis le CDN de
#: téléchargement (URL signée, **sans** jeton). Toute autre redirection est refusée.
GITHUB_ARCHIVE_HOSTS = frozenset({"api.github.com", "codeload.github.com"})
GITHUB_API_VERSION = "2022-11-28"
USER_AGENT = "waas-fleet-manager"

SOURCE_MODE_API = "api"
SOURCE_MODE_GIT = "git"

#: Archive compressée maximale remise à un conteneur (son /tmp fait 128 Mo).
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
#: Au-delà, l'archive en cours de téléchargement passe de la mémoire au disque.
SPOOL_MEMORY_BYTES = 8 * 1024 * 1024
DOWNLOAD_DEADLINE_SECONDS = 120.0
MAX_REDIRECTS = 3
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
CHUNK_BYTES = 64 * 1024

#: Un conteneur ne télécharge son dépôt qu'au démarrage (5 essais au plus) : au-delà,
#: c'est une boucle — ou un conteneur compromis — qui épuiserait le quota GitHub
#: partagé par la flotte. Chaque tentative compte, y compris les échecs.
RELAY_MAX_PER_TENANT = 20
RELAY_WINDOW_SECONDS = 600.0
#: Démarrage de masse (redémarrage de l'hôte) : téléchargements simultanés bornés.
RELAY_MAX_CONCURRENT = 4

_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


class _MaskSignedUrls(logging.Filter):
    """httpx journalise chaque requête en INFO : l'URL du CDN porte un jeton temporaire."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "codeload.github.com" in message:
            record.msg, record.args = re.sub(r"\?[^\s\"]*", "?<masqué>", message), ()
        return True


logging.getLogger("httpx").addFilter(_MaskSignedUrls())


class GithubRepo(NamedTuple):
    owner: str
    name: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


class RepoAccess(Enum):
    OK = "ok"
    NOT_FOUND = "not_found"        # inexistant, ou privé hors de portée du jeton
    UNAUTHORIZED = "unauthorized"  # jeton invalide ou expiré
    UNKNOWN = "unknown"            # GitHub injoignable, quota atteint… : ne bloque pas


class SourceError(Exception):
    """Échec de récupération d'une archive, avec le statut HTTP à renvoyer au conteneur."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def github_token() -> Optional[str]:
    """Jeton GitHub de la plateforme (lecture seule « Contents »), relu à chaque appel."""
    return (os.getenv(GITHUB_TOKEN_ENV) or "").strip() or None


def private_repos_enabled() -> bool:
    return github_token() is not None


def new_source_nonce() -> str:
    """Clé propre à **un** conteneur : un jeton de synchronisation seul (forgé, ou
    hérité d'un conteneur détruit au même identifiant) ne suffit pas à lire le dépôt."""
    return secrets.token_urlsafe(32)


def github_repository(repo_url: str) -> Optional[GithubRepo]:
    """``https://github.com/<propriétaire>/<dépôt>[.git]`` -> dépôt ; autre hôte -> ``None``.

    Lève ``ValueError`` pour une URL github.com qui ne désigne pas un dépôt
    (page ``/tree/…``, paramètres…) : le téléchargement de l'archive en dépend.
    """
    parsed = urlparse(repo_url)
    if (parsed.hostname or "").lower() != GITHUB_HOST:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) == 2 and parts[1].endswith(".git"):
        parts[1] = parts[1][:-4]
    valid = (
        not parsed.query and not parsed.fragment and not parsed.params and len(parts) == 2
        and _OWNER_RE.match(parts[0]) and _REPO_RE.match(parts[1]) and parts[1] not in (".", "..")
    )
    if not valid:
        raise ValueError(
            "URL GitHub invalide : attendu https://github.com/<propriétaire>/<dépôt>(.git), "
            "sans chemin supplémentaire ni paramètre."
        )
    return GithubRepo(parts[0], parts[1])


def source_mode(repo_url: str) -> str:
    """Mode de récupération du contenu pour un dépôt **déjà validé**."""
    try:
        repo = github_repository(repo_url)
    except ValueError:
        return SOURCE_MODE_GIT
    return SOURCE_MODE_API if repo and private_repos_enabled() else SOURCE_MODE_GIT


def http_client() -> httpx.Client:
    """Client sortant vers GitHub (point d'injection des tests)."""
    return httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=False, headers={"User-Agent": USER_AGENT})


def _api_headers(token: Optional[str]) -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": GITHUB_API_VERSION}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _repo_path(repo: GithubRepo) -> str:
    return f"/repos/{quote(repo.owner, safe='')}/{quote(repo.name, safe='')}"


# ---------------------------------------------------------
# VÉRIFICATION D'ACCÈS (DÉPLOIEMENT, FICHE, RECONSTRUCTION)
# ---------------------------------------------------------

def probe_github_repository(repo: GithubRepo, token: Optional[str]) -> RepoAccess:
    """Le dépôt est-il lisible avec ce jeton (ou anonymement sans jeton) ?"""
    try:
        with http_client() as client:
            response = client.get(f"{GITHUB_API}{_repo_path(repo)}", headers=_api_headers(token))
    except httpx.HTTPError as exc:
        logger.warning("Vérification du dépôt %s impossible (%s) : déploiement poursuivi.",
                       repo.slug, type(exc).__name__)
        return RepoAccess.UNKNOWN
    if response.status_code == 200:
        return RepoAccess.OK
    if response.status_code == 404:
        return RepoAccess.NOT_FOUND
    if response.status_code == 401:
        return RepoAccess.UNAUTHORIZED
    logger.warning("Vérification du dépôt %s : réponse GitHub %d (quota ?), déploiement poursuivi.",
                   repo.slug, response.status_code)
    return RepoAccess.UNKNOWN


def repo_access_error(repo_url: str) -> Optional[str]:
    """Message d'erreur si le dépôt est **certainement** illisible, sinon ``None``.

    Évite un conteneur qui redémarre en boucle sur un téléchargement refusé :
    l'erreur est donnée à la saisie de l'URL. Un résultat indéterminé (réseau,
    quota) ne bloque jamais : le conteneur réessaiera de lui-même.
    """
    repo = github_repository(repo_url)
    if repo is None:
        return None
    token = github_token()
    access = probe_github_repository(repo, token)
    if access is RepoAccess.UNAUTHORIZED:
        # Un jeton expiré ne doit pas bloquer un dépôt public : il reste servi anonymement.
        if probe_github_repository(repo, None) is not RepoAccess.NOT_FOUND:
            logger.warning("%s refusé par GitHub (invalide ou expiré) : dépôts privés indisponibles.",
                           GITHUB_TOKEN_ENV)
            return None
        return (f"Jeton GitHub de la plateforme ({GITHUB_TOKEN_ENV}) refusé : invalide ou expiré. "
                "Régénérez-le puis redémarrez l'API.")
    if access is RepoAccess.NOT_FOUND and token:
        return (f"Dépôt GitHub « {repo.slug} » inaccessible avec le jeton de la plateforme : vérifiez "
                "l'URL et ajoutez ce dépôt aux accès du jeton (Repository access, Contents : Read-only).")
    if access is RepoAccess.NOT_FOUND:
        return (f"Dépôt GitHub « {repo.slug} » introuvable ou privé. Pour un dépôt privé, définissez "
                f"{GITHUB_TOKEN_ENV} sur l'API (jeton fine-grained, Contents : Read-only) puis redémarrez-la.")
    return None


# ---------------------------------------------------------
# ARCHIVE REMISE AU CONTENEUR
# ---------------------------------------------------------

class RelayGate:
    """Admission des téléchargements : quota et exclusivité par locataire, total borné.

    Une place n'est occupée que pendant le téléchargement depuis GitHub (borné en
    durée) et toujours rendue par ``slot`` : un conteneur qui ne lit pas sa réponse
    ne bloque personne.
    """

    def __init__(self, per_tenant: int = RELAY_MAX_PER_TENANT, window: float = RELAY_WINDOW_SECONDS,
                 concurrent: int = RELAY_MAX_CONCURRENT) -> None:
        self._per_tenant, self._window, self._concurrent = per_tenant, window, concurrent
        self._history: dict[str, list[float]] = {}
        self._active: set[str] = set()
        self._lock = threading.Lock()

    def admit(self, tenant_id: str) -> None:
        """Réserve la place du locataire ; lève ``SourceError`` 429 ou 503 (non décomptée)."""
        now = time.monotonic()
        with self._lock:
            recent = [stamp for stamp in self._history.get(tenant_id, []) if now - stamp < self._window]
            if len(recent) >= self._per_tenant:
                logger.warning("Locataire '%s' : quota de téléchargement du dépôt atteint.", tenant_id)
                raise SourceError("Trop de téléchargements du dépôt pour ce locataire : réessayez plus tard.", 429)
            if tenant_id in self._active:
                raise SourceError("Téléchargement du dépôt déjà en cours pour ce locataire.", 429)
            if len(self._active) >= self._concurrent:
                raise SourceError("Téléchargements de dépôts saturés (démarrages simultanés) : réessayez.", 503)
            self._active = self._active | {tenant_id}
            self._history[tenant_id] = [*recent, now]

    def release(self, tenant_id: str) -> None:
        with self._lock:
            self._active = self._active - {tenant_id}

    @contextmanager
    def slot(self, tenant_id: str) -> Iterator[None]:
        self.admit(tenant_id)
        try:
            yield
        finally:
            self.release(tenant_id)

    def in_flight(self) -> int:
        with self._lock:
            return len(self._active)


relay_gate = RelayGate()


def download_archive(repo: GithubRepo, token: Optional[str]) -> tuple[BinaryIO, int]:
    """Télécharge **entièrement** l'archive ``tar.gz`` de la branche par défaut.

    Rien n'est envoyé au conteneur avant la fin : une archive tronquée, trop
    lourde ou trop lente devient une erreur HTTP explicite (jamais un site
    partiel), et la réponse porte une longueur exacte. Renvoie le fichier
    (positionné au début) et sa taille. Lève ``SourceError``.
    """
    with http_client() as client:
        response = _open_archive(client, repo, token)
        spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY_BYTES)
        try:
            size = _copy_bounded(response, spool, repo)
        except BaseException:
            spool.close()
            raise
        finally:
            response.close()
    spool.seek(0)
    return spool, size


def iter_file(handle: BinaryIO) -> Iterator[bytes]:
    """Octets d'un fichier, fermé à la fin de l'envoi."""
    try:
        while chunk := handle.read(CHUNK_BYTES):
            yield chunk
    finally:
        handle.close()


def _open_archive(client: httpx.Client, repo: GithubRepo, token: Optional[str]) -> httpx.Response:
    response = _follow_archive_redirects(client, repo, token)
    token_rejected = response.status_code == 401 and token is not None
    if token_rejected:
        # Jeton expiré ou révoqué : un dépôt public doit rester servi.
        response.close()
        logger.warning("%s refusé par GitHub (invalide ou expiré) : archive de %s demandée anonymement.",
                       GITHUB_TOKEN_ENV, repo.slug)
        response = _follow_archive_redirects(client, repo, None)
    if response.status_code != 200:
        # Privé + jeton refusé : c'est le jeton qu'il faut corriger, pas le dépôt.
        upstream = 401 if token_rejected and response.status_code == 404 else response.status_code
        response.close()
        raise _archive_error(upstream, repo, token)
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_ARCHIVE_BYTES:
        response.close()
        raise _too_large(repo)
    return response


def _follow_archive_redirects(client: httpx.Client, repo: GithubRepo, token: Optional[str]) -> httpx.Response:
    url = f"{GITHUB_API}{_repo_path(repo)}/tarball"
    for _ in range(MAX_REDIRECTS + 1):
        target = urlparse(url)
        if target.scheme != "https" or target.hostname not in GITHUB_ARCHIVE_HOSTS:
            # L'URL n'est jamais journalisée : celle du CDN porte un jeton temporaire.
            raise SourceError(f"Redirection de l'archive de {repo.slug} vers un hôte non autorisé.", 502)
        # Le jeton n'est présenté qu'à l'API GitHub, jamais au CDN.
        headers = _api_headers(token if target.hostname == "api.github.com" else None)
        try:
            response = client.send(client.build_request("GET", url, headers=headers), stream=True)
        except httpx.HTTPError as exc:
            raise SourceError(f"GitHub injoignable ({type(exc).__name__}).", 502) from None
        if not response.is_redirect:
            return response
        location = response.headers.get("location", "")
        response.close()
        url = urljoin(url, location)
    raise SourceError(f"Trop de redirections pour l'archive de {repo.slug}.", 502)


def _copy_bounded(response: httpx.Response, spool: BinaryIO, repo: GithubRepo) -> int:
    deadline = time.monotonic() + DOWNLOAD_DEADLINE_SECONDS
    size = 0
    try:
        for chunk in response.iter_bytes(CHUNK_BYTES):
            size += len(chunk)
            if size > MAX_ARCHIVE_BYTES:
                raise _too_large(repo)
            if time.monotonic() > deadline:
                raise SourceError(f"Téléchargement de l'archive de {repo.slug} trop long.", 504)
            spool.write(chunk)
    except httpx.HTTPError as exc:
        raise SourceError(f"Téléchargement de l'archive de {repo.slug} interrompu ({type(exc).__name__}).",
                          502) from None
    return size


def _too_large(repo: GithubRepo) -> SourceError:
    return SourceError(f"Archive de {repo.slug} au-delà de {MAX_ARCHIVE_BYTES // (1024 * 1024)} Mo.", 413)


def _archive_error(upstream: int, repo: GithubRepo, token: Optional[str]) -> SourceError:
    if upstream == 404:
        scope = "hors de portée du jeton GitHub" if token else f"privé ({GITHUB_TOKEN_ENV} absent)"
        return SourceError(f"Dépôt {repo.slug} introuvable ou {scope}.", 404)
    if upstream == 401:
        return SourceError(f"Jeton GitHub ({GITHUB_TOKEN_ENV}) refusé : invalide ou expiré.", 502)
    if upstream in (403, 429):
        return SourceError("GitHub refuse temporairement le téléchargement (quota atteint ?).", 503)
    return SourceError(f"Réponse GitHub inattendue ({upstream}) pour l'archive de {repo.slug}.", 502)
