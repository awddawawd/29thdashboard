"""Dépôts GitHub privés : le jeton reste dans l'API, jamais dans le conteneur.

Couvre la validation des URL, le mode de récupération transmis au conteneur,
la vérification d'accès à la saisie (déploiement, fiche, reconstruction), la
remise de l'archive par l'API (clé du conteneur, admission, taille, coupures,
jeton expiré) et le contrat réel du script du conteneur (extraction sûre,
archive tronquée, nouvel essai, refus définitifs).

Exécution : .venv\\Scripts\\python.exe tests\\test_private_repos.py
"""

from __future__ import annotations

import http.server
import importlib.util
import io
import json
import logging
import os
import pathlib
import socketserver
import sys
import tarfile
import tempfile
import threading
import zlib
from types import SimpleNamespace
from typing import Any, Iterator, Optional

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "End"))
os.chdir(ROOT / "End")

os.environ["WAAS_SYNC_SECRET"] = "secret-de-test-depots-prives"
os.environ.pop("WAAS_GITHUB_TOKEN", None)

import httpx  # noqa: E402
import git_source  # noqa: E402
import sync_auth  # noqa: E402
import main  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

GITHUB_TOKEN = "github_pat_TEST_ne_doit_jamais_quitter_l_api"
PRIVATE_REPO = "https://github.com/agence/client-prive.git"
NONCE = "cle-de-source-du-conteneur-boutique-a"

FAILURES: list[str] = []
CHECKS = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(f"{name} {detail}")
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f" -> {detail}" if detail and not condition else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


class Patch:
    """Remplace des attributs le temps d'un bloc ``with`` (restauration garantie)."""

    def __init__(self, target: object, **values: object) -> None:
        self.target, self.values, self.saved = target, values, {}

    def __enter__(self) -> "Patch":
        for name, value in self.values.items():
            self.saved[name] = getattr(self.target, name)
            setattr(self.target, name, value)
        return self

    def __exit__(self, *exc: object) -> None:
        for name, value in self.saved.items():
            setattr(self.target, name, value)


class GithubToken:
    """Définit (ou retire) ``WAAS_GITHUB_TOKEN`` le temps d'un bloc."""

    def __init__(self, value: Optional[str]) -> None:
        self.value, self.saved = value, None

    def __enter__(self) -> None:
        self.saved = os.environ.get(git_source.GITHUB_TOKEN_ENV)
        if self.value is None:
            os.environ.pop(git_source.GITHUB_TOKEN_ENV, None)
        else:
            os.environ[git_source.GITHUB_TOKEN_ENV] = self.value

    def __exit__(self, *exc: object) -> None:
        if self.saved is None:
            os.environ.pop(git_source.GITHUB_TOKEN_ENV, None)
        else:
            os.environ[git_source.GITHUB_TOKEN_ENV] = self.saved


ROOT_DIR = "agence-client-prive-abc1234"


def github_tar(files: dict[str, bytes], extra: Optional[list[tarfile.TarInfo]] = None) -> bytes:
    """Tar **non compressé** au format de GitHub : dossier racine + en-tête pax global."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT,
                      pax_headers={"comment": "abc1234def5678"}) as archive:
        directory = tarfile.TarInfo(f"{ROOT_DIR}/")
        directory.type, directory.mode = tarfile.DIRTYPE, 0o755
        archive.addfile(directory)
        for name, data in files.items():
            info = tarfile.TarInfo(f"{ROOT_DIR}/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for info in extra or []:
            archive.addfile(info)
    return buffer.getvalue()


def gzip_bytes(data: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=31)
    return compressor.compress(data) + compressor.flush()


def github_archive(files: dict[str, bytes], extra: Optional[list[tarfile.TarInfo]] = None) -> bytes:
    return gzip_bytes(github_tar(files, extra))


SITE_FILES = {"index.html": b"<html><body><h1>Client</h1></body></html>",
              "assets/app.css": b"body{margin:0}", "contact.html": b"<p>Contact</p>"}
ARCHIVE = github_archive(SITE_FILES)


def cut_between_members() -> bytes:
    """gzip **sans fin de flux** dont le contenu s'arrête pile après le premier fichier :
    tarfile seul y voit une archive complète (site partiel)."""
    raw = github_tar(SITE_FILES)
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        first = next(m for m in archive.getmembers() if m.isfile())
    boundary = first.offset_data + ((first.size + 511) // 512) * 512
    compressor = zlib.compressobj(wbits=31)
    return compressor.compress(raw[:boundary]) + compressor.flush(zlib.Z_SYNC_FLUSH)


class ChunkStream(httpx.SyncByteStream):
    """Corps sans Content-Length, éventuellement coupé par une erreur réseau."""

    def __init__(self, chunks: list[bytes], fail_after: bool = False) -> None:
        self.chunks, self.fail_after = chunks, fail_after

    def __iter__(self) -> Iterator[bytes]:
        yield from self.chunks
        if self.fail_after:
            raise httpx.ReadError("connexion coupée")


class FakeGithub:
    """GitHub simulé (API + CDN codeload) ; réponses distinctes avec et sans jeton."""

    def __init__(self, repo_status: int = 200, tarball_status: int = 302,
                 anonymous_repo_status: Optional[int] = None, anonymous_tarball_status: Optional[int] = None,
                 redirect_to: str = "https://codeload.github.com/agence/client-prive/legacy.tar.gz/main?token=TMP",
                 archive: Optional[httpx.Response] = None, fail_network: bool = False) -> None:
        self.status = {True: (repo_status, tarball_status),
                       False: (anonymous_repo_status or repo_status, anonymous_tarball_status or tarball_status)}
        self.redirect_to, self.fail_network = redirect_to, fail_network
        self.archive = archive or httpx.Response(200, content=ARCHIVE)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_network:
            raise httpx.ConnectError("réseau coupé", request=request)
        repo_status, tarball_status = self.status["authorization" in request.headers]
        host, path = request.url.host, request.url.path
        if host == "api.github.com" and path.endswith("/tarball"):
            if tarball_status == 302:
                return httpx.Response(302, headers={"location": self.redirect_to})
            return httpx.Response(tarball_status)
        if host == "api.github.com":
            return httpx.Response(repo_status, json={})
        if host == "codeload.github.com":
            return self.archive
        return httpx.Response(599)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler), follow_redirects=False)

    def authorization(self, host: str) -> list[Optional[str]]:
        return [r.headers.get("authorization") for r in self.requests if r.url.host == host]


# ---------------------------------------------------------------------------
# 1. URL de dépôt
# ---------------------------------------------------------------------------

def test_url_parsing() -> None:
    section("1. URL de dépôt GitHub : propriétaire/dépôt stricts")

    for url in ("https://github.com/agence/client-prive.git", "https://github.com/agence/client-prive",
                "https://github.com/agence/client-prive/"):
        check(f"dépôt reconnu : {url}",
              git_source.github_repository(url) == ("agence", "client-prive"), str(git_source.github_repository(url)))
    check("autre hôte : non GitHub", git_source.github_repository("https://gitlab.com/g/sous/t.git") is None)

    for url in ("https://github.com/agence", "https://github.com/agence/site/tree/main",
                "https://github.com/agence/site.git?ref=x", "https://github.com/agence/site#readme",
                "https://github.com/agence/..", "https://github.com/-agence/site"):
        try:
            git_source.github_repository(url)
            check(f"URL GitHub refusée : {url}", False, "acceptée")
        except ValueError:
            check(f"URL GitHub refusée : {url}", True)
        try:
            main.validate_repo_url(url)
            check(f"validate_repo_url -> 422 : {url}", False, "acceptée")
        except HTTPException as exc:
            check(f"validate_repo_url -> 422 : {url}", exc.status_code == 422, str(exc.status_code))

    try:
        main.validate_repo_url("https://gitlab.com/groupe/sous-groupe/t.git")
        check("GitLab (sous-groupes) toujours accepté", True)
    except HTTPException as exc:
        check("GitLab (sous-groupes) toujours accepté", False, exc.detail)


# ---------------------------------------------------------------------------
# 2. Mode de récupération et environnement du conteneur
# ---------------------------------------------------------------------------

def _provisioned_environment(repo_url: str) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class FakeContainers:
        def run(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(reload=lambda: None)

    class FakeNetworks:
        def get(self, name: str) -> None:
            return None

    settings = {"api_public_url": "http://host.docker.internal:8000", "browser_api_url": "http://localhost:8000"}
    with Patch(main, docker_client=SimpleNamespace(containers=FakeContainers(), networks=FakeNetworks())), \
            Patch(main.db, get_setting=lambda key, default=None: settings.get(key, default if default is not None else "")):
        main.provision_container("boutique-a", repo_url)
    return captured.get("environment") or {}


def test_source_mode_and_environment() -> None:
    section("2. Mode de récupération transmis au conteneur, sans jeton GitHub")

    with GithubToken(None):
        check("sans jeton : GitHub cloné par git (public)", git_source.source_mode(PRIVATE_REPO) == "git")
        env = main.build_tenant_environment("boutique-a", PRIVATE_REPO, "https://api.waas.example")
        check("sans jeton : SOURCE_MODE=git, sans clé de source",
              env.get("SOURCE_MODE") == "git" and "SOURCE_NONCE" not in env, str(sorted(env)))

    with GithubToken(GITHUB_TOKEN):
        check("avec jeton : GitHub remis par l'API", git_source.source_mode(PRIVATE_REPO) == "api")
        check("avec jeton : GitLab reste cloné par git",
              git_source.source_mode("https://gitlab.com/g/t.git") == "git")
        env = _provisioned_environment(PRIVATE_REPO)
        dumped = json.dumps(env)
        check("conteneur provisionné en SOURCE_MODE=api", env.get("SOURCE_MODE") == "api", str(env.get("SOURCE_MODE")))
        check("jeton GitHub absent de l'environnement du conteneur", GITHUB_TOKEN not in dumped)
        check("aucune variable GITHUB/GIT_TOKEN transmise",
              not any("GITHUB" in key.upper() or ("TOKEN" in key.upper() and key != "SYNC_TOKEN") for key in env),
              str(sorted(env)))
        check("URL du dépôt sans identifiant", env.get("REPO_URL") == PRIVATE_REPO and "@" not in env["REPO_URL"])
        check("jeton de synchronisation scopé au locataire",
              sync_auth.verify_tenant_token(env.get("SYNC_TOKEN", "")) == "boutique-a")
        other = _provisioned_environment(PRIVATE_REPO)
        check("clé de source aléatoire, propre à chaque conteneur",
              len(env.get("SOURCE_NONCE", "")) >= 32 and env.get("SOURCE_NONCE") != other.get("SOURCE_NONCE"))

    check("jeton de synchronisation non ASCII : refusé sans erreur",
          sync_auth.verify_tenant_token("boutique-a.signature-é") is None)


# ---------------------------------------------------------------------------
# 3. Vérification d'accès à la saisie de l'URL
# ---------------------------------------------------------------------------

def _access_error(fake: FakeGithub, repo_url: str = PRIVATE_REPO) -> Optional[str]:
    with Patch(git_source, http_client=fake.client):
        return git_source.repo_access_error(repo_url)


def test_access_check() -> None:
    section("3. Accès au dépôt vérifié avant tout conteneur")

    with GithubToken(None):
        fake = FakeGithub(repo_status=200)
        check("dépôt public sans jeton : accepté", _access_error(fake) is None)
        check("sans jeton : requête anonyme", fake.authorization("api.github.com") == [None])

        message = _access_error(FakeGithub(repo_status=404)) or ""
        check("privé sans jeton : erreur explicite", "WAAS_GITHUB_TOKEN" in message and "privé" in message, message)

    with GithubToken(GITHUB_TOKEN):
        fake = FakeGithub(repo_status=200)
        check("privé avec jeton : accepté", _access_error(fake) is None)
        check("jeton présenté à l'API GitHub", fake.authorization("api.github.com") == [f"Bearer {GITHUB_TOKEN}"])

        message = _access_error(FakeGithub(repo_status=404)) or ""
        check("hors de portée du jeton : erreur explicite", "accès du jeton" in message, message)
        check("message sans le jeton", GITHUB_TOKEN not in message)

        message = _access_error(FakeGithub(repo_status=401, anonymous_repo_status=404)) or ""
        check("privé + jeton expiré : erreur explicite", "invalide ou expiré" in message, message)
        check("public + jeton expiré : non bloquant",
              _access_error(FakeGithub(repo_status=401, anonymous_repo_status=200)) is None)

        check("quota GitHub (403) : non bloquant", _access_error(FakeGithub(repo_status=403)) is None)
        check("GitHub injoignable : non bloquant", _access_error(FakeGithub(fail_network=True)) is None)

        fake = FakeGithub()
        check("hôte non GitHub : aucune requête", _access_error(fake, "https://gitlab.com/g/t.git") is None
              and not fake.requests)

        with Patch(git_source, http_client=FakeGithub(repo_status=404).client):
            try:
                main.check_repo_access(PRIVATE_REPO)
                check("check_repo_access -> 422", False, "accepté")
            except HTTPException as exc:
                check("check_repo_access -> 422", exc.status_code == 422 and "accès du jeton" in exc.detail,
                      f"{exc.status_code} {exc.detail}")


def test_entry_points() -> None:
    section("4. Contrôle appliqué au déploiement, à la fiche et à « Déployer »")

    probes: list[str] = []

    def refusing(repo_url: str) -> str:
        probes.append(repo_url)
        return "Dépôt inaccessible (test)"

    def accepting(repo_url: str) -> None:
        probes.append(repo_url)
        return None

    # --- Fiche : GitHub n'est consulté que si le dépôt change ---------------
    saved: list[dict[str, Any]] = []
    entry = {"site_id": "site-a", "repo_url": "https://github.com/agence/ancien.git"}
    with Patch(main, require_site_entry=lambda sid: entry, find_site_entry=lambda sid: entry,
               _invalidate_cache=lambda: None), \
            Patch(main.db, upsert_site_record=lambda sid, fields: saved.append(fields) or fields):
        with Patch(git_source, repo_access_error=accepting):
            main.admin_update_site("site-a", main.SiteUpdateRequest(repo_url=entry["repo_url"], notes="x"))
            check("fiche : dépôt inchangé -> aucune requête GitHub", probes == [], str(probes))
            main.admin_update_site("site-a", main.SiteUpdateRequest(repo_url=PRIVATE_REPO))
            check("fiche : nouveau dépôt vérifié", probes == [PRIVATE_REPO], str(probes))
        saved.clear()
        with Patch(git_source, repo_access_error=refusing):
            try:
                main.admin_update_site("site-a", main.SiteUpdateRequest(repo_url="https://github.com/agence/autre.git"))
                check("fiche : dépôt inaccessible refusé (422)", False, "enregistré")
            except HTTPException as exc:
                check("fiche : dépôt inaccessible refusé (422)", exc.status_code == 422)
            check("fiche : rien n'est enregistré", saved == [], str(saved))

    # --- Déploiement : refus avant toute création de conteneur ---------------
    provisioned: list[str] = []
    docker = SimpleNamespace(containers=SimpleNamespace(list=lambda **kwargs: []))
    with Patch(main, docker_client=docker, api_public_url_setting=lambda: "http://172.17.0.1:8000",
               provision_container=lambda sid, url: provisioned.append(sid)), \
            Patch(git_source, repo_access_error=refusing):
        try:
            main.deploy_tenant(main.DeployTenantRequest(site_id="boutique-a", github_url=PRIVATE_REPO))
            check("déploiement : dépôt inaccessible refusé (422)", False, "déployé")
        except HTTPException as exc:
            check("déploiement : dépôt inaccessible refusé (422)", exc.status_code == 422, str(exc.status_code))
    check("déploiement : aucun conteneur créé", provisioned == [], str(provisioned))

    # --- Reconstruction / « Déployer » d'un site non configuré ---------------
    events: list[str] = []
    existing = SimpleNamespace(status="running", attrs={"Config": {"Env": []}},
                               rename=lambda name: events.append("rename"), stop=lambda timeout=10: events.append("stop"))
    docker = SimpleNamespace(containers=SimpleNamespace(get=lambda name: existing))
    with Patch(main, docker_client=docker, api_public_url_setting=lambda: "http://172.17.0.1:8000",
               provision_container=lambda sid, url: provisioned.append(sid)), \
            Patch(main.db, get_site_record=lambda sid: {"repo_url": PRIVATE_REPO}), \
            Patch(git_source, repo_access_error=refusing):
        try:
            main.rebuild_site("site-a")
            check("reconstruction : dépôt inaccessible refusé (422)", False, "reconstruit")
        except HTTPException as exc:
            check("reconstruction : dépôt inaccessible refusé (422)", exc.status_code == 422)
    check("reconstruction : conteneur existant intact", events == [] and provisioned == [], str(events))


# ---------------------------------------------------------------------------
# 5. Remise de l'archive par l'API
# ---------------------------------------------------------------------------

def test_source_endpoint() -> None:
    section("5. /internal/sync/source : archive du seul conteneur authentifié")

    envs = {
        "waas-boutique-a": [f"REPO_URL={PRIVATE_REPO}", "SOURCE_MODE=api", f"SOURCE_NONCE={NONCE}"],
        "waas-boutique-gitlab": ["REPO_URL=https://gitlab.com/g/t.git", "SOURCE_NONCE=cle-gitlab"],
        "waas-boutique-git": [f"REPO_URL={PRIVATE_REPO}", "SOURCE_MODE=git"],
    }

    def get(name: str) -> Any:
        if name not in envs:
            raise main.NotFound(name)
        return SimpleNamespace(attrs={"Config": {"Env": envs[name]}})

    client = TestClient(main.app)
    path = "/internal/sync/source"
    gate = git_source.RelayGate(per_tenant=100)

    def headers(tenant: str = "boutique-a", nonce: Optional[str] = NONCE) -> dict[str, str]:
        values = {"X-WAAS-Sync-Token": sync_auth.issue_tenant_token(tenant)}
        if nonce is not None:
            values["X-WAAS-Source-Nonce"] = nonce
        return values

    def call(fake: FakeGithub, request_headers: Optional[dict[str, str]] = None, url: str = path,
             **git_patches: Any) -> httpx.Response:
        with Patch(main, docker_client=SimpleNamespace(containers=SimpleNamespace(get=get))), \
                Patch(git_source, http_client=fake.client, relay_gate=gate, **git_patches):
            return client.get(url, headers=request_headers if request_headers is not None else headers())

    with GithubToken(GITHUB_TOKEN):
        answer = call(FakeGithub(), {})
        check("sans jeton de synchronisation : 401", answer.status_code == 401, str(answer.status_code))

        fake = FakeGithub()
        answer = call(fake, headers(nonce=None))
        check("jeton seul, sans clé du conteneur : 401", answer.status_code == 401 and not fake.requests,
              str(answer.status_code))
        answer = call(fake, headers(nonce="cle-d-un-ancien-conteneur"))
        check("clé d'un autre conteneur : 401", answer.status_code == 401 and not fake.requests,
              str(answer.status_code))

        fake = FakeGithub()
        answer = call(fake, url=f"{path}?repo=https://github.com/autre/secret.git&tenant_id=boutique-b")
        check("archive remise (200)", answer.status_code == 200 and answer.content == ARCHIVE,
              f"{answer.status_code} {answer.text[:120]}")
        check("longueur exacte annoncée (coupure détectable)",
              answer.headers.get("content-length") == str(len(ARCHIVE)), str(answer.headers.get("content-length")))
        check("type gzip, jamais mis en cache", answer.headers.get("content-type") == "application/gzip"
              and answer.headers.get("cache-control") == "no-store", str(dict(answer.headers)))
        check("dépôt du conteneur appelant (paramètres ignorés)",
              [r.url.path for r in fake.requests if r.url.host == "api.github.com"]
              == ["/repos/agence/client-prive/tarball"], str([str(r.url) for r in fake.requests]))
        check("jeton GitHub présenté à l'API seulement",
              fake.authorization("api.github.com") == [f"Bearer {GITHUB_TOKEN}"]
              and fake.authorization("codeload.github.com") == [None])
        check("jeton GitHub absent de la réponse",
              GITHUB_TOKEN not in answer.text and GITHUB_TOKEN not in json.dumps(dict(answer.headers)))

        answer = call(FakeGithub(), headers("boutique-b", "x"))
        check("conteneur détruit : 404 (aucun repli sur la fiche)", answer.status_code == 404, str(answer.status_code))
        answer = call(FakeGithub(), headers("boutique-git", ""))
        check("conteneur en mode git (sans clé) : 401", answer.status_code == 401, str(answer.status_code))
        answer = call(FakeGithub(), headers("boutique-gitlab", "cle-gitlab"))
        check("dépôt hors GitHub : 409 (cloné par git)", answer.status_code == 409, str(answer.status_code))

        answer = call(FakeGithub(tarball_status=404))
        check("dépôt hors de portée du jeton : 404", answer.status_code == 404, str(answer.status_code))

        fake = FakeGithub(tarball_status=401, anonymous_tarball_status=302)
        answer = call(fake)
        check("jeton expiré, dépôt public : servi anonymement", answer.status_code == 200
              and fake.authorization("api.github.com") == [f"Bearer {GITHUB_TOKEN}", None], str(answer.status_code))
        answer = call(FakeGithub(tarball_status=401, anonymous_tarball_status=404))
        check("jeton expiré, dépôt privé : 502 (jeton à corriger)",
              answer.status_code == 502 and "expiré" in answer.text, f"{answer.status_code} {answer.text[:120]}")

        fake = FakeGithub(redirect_to="https://169.254.169.254/latest/meta-data/")
        answer = call(fake)
        check("redirection hors GitHub refusée (anti-SSRF)", answer.status_code == 502
              and not any(r.url.host == "169.254.169.254" for r in fake.requests), str(answer.status_code))

        oversized = httpx.Response(200, headers={"content-length": str(git_source.MAX_ARCHIVE_BYTES + 1)})
        answer = call(FakeGithub(archive=oversized))
        check("archive annoncée trop volumineuse : 413", answer.status_code == 413, str(answer.status_code))
        unannounced = httpx.Response(200, stream=ChunkStream([ARCHIVE[:100], ARCHIVE[100:]]))
        answer = call(FakeGithub(archive=unannounced), MAX_ARCHIVE_BYTES=120)
        check("archive trop volumineuse sans longueur annoncée : 413 avant tout envoi",
              answer.status_code == 413, str(answer.status_code))
        dropped = httpx.Response(200, stream=ChunkStream([ARCHIVE[:50]], fail_after=True))
        answer = call(FakeGithub(archive=dropped))
        check("coupure GitHub en plein flux : 502, rien de partiel transmis",
              answer.status_code == 502 and answer.headers.get("content-type") != "application/gzip",
              f"{answer.status_code} {answer.headers.get('content-type')}")

        spec = client.get("/openapi.json").json()
        check("endpoint absent de la documentation publique", path not in spec.get("paths", {}))

    with GithubToken(None):
        fake = FakeGithub()
        answer = call(fake)
        check("jeton retiré : dépôt public toujours servi (anonyme)",
              answer.status_code == 200 and fake.authorization("api.github.com") == [None], str(answer.status_code))

    check("places rendues après succès, erreurs et refus", gate.in_flight() == 0, str(gate.in_flight()))

    with GithubToken(GITHUB_TOKEN):
        gate = git_source.RelayGate(per_tenant=2)
        codes = [call(FakeGithub(tarball_status=503 if i == 0 else 302)).status_code for i in range(3)]
        check("boucle de téléchargements : 429, échecs compris", codes == [502, 200, 429], str(codes))
        answer = call(FakeGithub(), headers("boutique-gitlab", "cle-gitlab"))
        check("quota propre à chaque locataire", answer.status_code == 409, str(answer.status_code))


def test_relay_gate() -> None:
    section("5b. Porte des téléchargements : quota, exclusivité, total borné")

    gate = git_source.RelayGate(per_tenant=3, window=600.0, concurrent=2)

    def admit(tenant: str) -> Any:
        try:
            gate.admit(tenant)
            return "ok"
        except git_source.SourceError as exc:
            return exc.status_code

    check("deux locataires simultanés admis", [admit("a"), admit("b")] == ["ok", "ok"] and gate.in_flight() == 2)
    check("même locataire déjà en cours : 429", admit("a") == 429)
    check("total atteint : 503", admit("c") == 503)
    gate.release("a")
    check("place rendue : nouveau locataire admis", admit("c") == "ok")
    gate.release("b")
    gate.release("c")
    gate.release("inconnu")                            # sans effet

    # « c » : un refus pour saturation (non décompté) puis 1 admission ; quota = 3.
    results = []
    for _ in range(3):
        outcome = admit("c")
        results.append(outcome)
        if outcome == "ok":
            gate.release("c")
    check("saturation non décomptée, puis quota par locataire : 429", results == ["ok", "ok", 429], str(results))
    check("autre locataire non affecté", admit("d") == "ok")
    gate.release("d")

    try:
        with gate.slot("e"):
            raise RuntimeError("échec pendant le téléchargement")
    except RuntimeError:
        pass
    check("place rendue même sur exception", gate.in_flight() == 0, str(gate.in_flight()))


# ---------------------------------------------------------------------------
# 6. Script du conteneur : contrat HTTP réel et extraction sûre
# ---------------------------------------------------------------------------

class _StubSource(http.server.BaseHTTPRequestHandler):
    responses: list[tuple[int, bytes]] = []
    calls: list[tuple[Optional[str], Optional[str]]] = []
    user_agents: list[Optional[str]] = []

    def log_message(self, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        cls = type(self)
        cls.calls.append((self.headers.get("X-WAAS-Sync-Token"), self.headers.get("X-WAAS-Source-Nonce")))
        cls.user_agents.append(self.headers.get("User-Agent"))
        code, body = cls.responses.pop(0) if len(cls.responses) > 1 else cls.responses[0]
        self.send_response(code)
        self.send_header("Content-Type", "application/gzip" if code == 200 else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _load_fetcher() -> Any:
    spec = importlib.util.spec_from_file_location("waas_fetch_source_under_test",
                                                  ROOT / "waas-base-image" / "fetch_source.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _malicious_members() -> list[tarfile.TarInfo]:
    traversal = tarfile.TarInfo(f"{ROOT_DIR}/../../evil.txt")
    link = tarfile.TarInfo(f"{ROOT_DIR}/passwd")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    return [traversal, link]


def test_fetcher_contract() -> None:
    section("6. fetch_source.py (conteneur) : téléchargement authentifié et extraction sûre")

    fetcher = _load_fetcher()
    sleeps: list[float] = []
    token = sync_auth.issue_tenant_token("boutique-a")

    with socketserver.TCPServer(("127.0.0.1", 0), _StubSource) as server, \
            Patch(fetcher, time=SimpleNamespace(sleep=sleeps.append)):
        threading.Thread(target=server.serve_forever, daemon=True).start()
        api = f"http://127.0.0.1:{server.server_address[1]}"

        def run(responses: list[tuple[int, bytes]], attempts: int = 3) -> tuple[Any, pathlib.Path, pathlib.Path]:
            _StubSource.responses, _StubSource.calls = list(responses), []
            _StubSource.user_agents = []
            sleeps.clear()
            sandbox = pathlib.Path(tempfile.mkdtemp())
            destination = sandbox / "src" / "site"
            try:
                return fetcher.fetch_source(destination, api, token, NONCE, attempts=attempts), sandbox, destination
            except fetcher.FetchError as exc:
                return exc, sandbox, destination

        result, _, destination = run([(200, ARCHIVE)])
        check("commit GitHub restitué", result == "abc1234def5678", repr(result))
        check("dossier racine de l'archive retiré",
              all((destination / name).is_file() for name in SITE_FILES),
              str(sorted(p.name for p in destination.rglob("*"))))
        check("jeton et clé du conteneur présentés", _StubSource.calls == [(token, NONCE)], str(_StubSource.calls))
        check("en-tête User-Agent personnalisé (anti-blocage Cloudflare WAF)",
              _StubSource.user_agents and all("Python-urllib" not in str(ua) and "WaaS-FetchSource" in str(ua)
                                              for ua in _StubSource.user_agents),
              str(_StubSource.user_agents))

        hostile = github_archive({"index.html": b"<h1>ok</h1>"}, extra=_malicious_members())
        result, sandbox, destination = run([(200, hostile)])
        check("archive hostile : fichiers légitimes extraits", (destination / "index.html").is_file(), repr(result))
        check("traversée de chemin neutralisée", not any(p.name == "evil.txt" for p in sandbox.rglob("*")))
        check("lien absolu vers le système ignoré", not (destination / "passwd").exists()
              and not (destination / "passwd").is_symlink())

        result, _, _ = run([(503, b'{"detail":"GitHub"}'), (200, ARCHIVE)])
        check("erreur transitoire : nouvel essai puis succès", result == "abc1234def5678" and len(sleeps) == 1,
              f"{result!r} {sleeps}")

        result, _, _ = run([(200, ARCHIVE[: len(ARCHIVE) // 2])], attempts=2)
        check("archive coupée en plein fichier : rejetée après les essais",
              isinstance(result, fetcher.FetchError) and len(_StubSource.calls) == 2, repr(result))

        partial_tar = zlib.decompressobj(wbits=31).decompress(cut_between_members())
        with tarfile.open(fileobj=io.BytesIO(partial_tar), mode="r|") as probe:
            accepted_alone = sum(1 for member in probe if member.isfile())
        result, _, destination = run([(200, cut_between_members())], attempts=1)
        check("coupure entre deux fichiers : tarfile seul l'accepterait", accepted_alone == 1, str(accepted_alone))
        check("coupure entre deux fichiers : rejetée (fin du flux gzip absente)",
              isinstance(result, fetcher.FetchError), repr(result))

        corrupted = ARCHIVE[:-8] + bytes([ARCHIVE[-8] ^ 0xFF]) + ARCHIVE[-7:]   # CRC32 du flux altéré
        result, _, _ = run([(200, corrupted)], attempts=1)
        check("somme de contrôle gzip invalide : rejetée", isinstance(result, fetcher.FetchError), repr(result))

        result, _, _ = run([(404, '{"detail":"Dépôt introuvable"}'.encode("utf-8"))])
        check("refus définitif (404) : aucun nouvel essai", isinstance(result, fetcher.FetchError)
              and len(_StubSource.calls) == 1 and "introuvable" in str(result), f"{result!r}")

        with Patch(fetcher, MAX_EXTRACTED_BYTES=10):
            result, _, _ = run([(200, ARCHIVE)])
        check("dépôt trop volumineux : refusé sans nouvel essai", isinstance(result, fetcher.FetchError)
              and len(_StubSource.calls) == 1, repr(result))

        server.shutdown()

    versions = {(3, 11, 12): False, (3, 11, 13): True, (3, 12, 10): False, (3, 12, 11): True,
                (3, 13, 4): True, (3, 10, 99): False, (3, 14, 0): True}
    check("extraction refusée sur un Python non corrigé (CVE-2025-4517)",
          all(fetcher.tarfile_is_safe(v) is expected for v, expected in versions.items()),
          str({v: fetcher.tarfile_is_safe(v) for v in versions}))

    saved = {key: os.environ.pop(key, None) for key in ("SYNC_TOKEN", "SOURCE_NONCE", "API_BASE_URL")}
    os.environ.update({"SYNC_TOKEN": token, "API_BASE_URL": "http://127.0.0.1:9"})
    try:
        check("clé de source absente : arrêt immédiat", fetcher.main(["fetch_source.py", "/tmp/x"]) == 1)
    finally:
        for key, value in saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value

    sources = {name: (ROOT / "waas-base-image" / name).read_text(encoding="utf-8")
               for name in ("entrypoint.sh", "Dockerfile", "fetch_source.py", "sync_engine.py")}
    check("aucun jeton GitHub référencé dans l'image",
          not any("WAAS_GITHUB_TOKEN" in text or "Authorization" in text for text in sources.values()))
    check("point d'entrée : branche SOURCE_MODE=api",
          'SOURCE_MODE" = "api"' in sources["entrypoint.sh"] and "fetch_source.py" in sources["entrypoint.sh"])
    check("image : script copié", "COPY fetch_source.py" in sources["Dockerfile"])


def test_signed_url_masking() -> None:
    section("7. Journaux : URL signée du CDN masquée")

    httpx_logger = logging.getLogger("httpx")
    record = httpx_logger.makeRecord("httpx", logging.INFO, __file__, 0, 'HTTP Request: %s %s "%s"',
                                     ("GET", "https://codeload.github.com/o/r/legacy.tar.gz/main?token=SECRET", "200"),
                                     None)
    for log_filter in httpx_logger.filters:
        log_filter.filter(record)
    message = record.getMessage()
    check("jeton temporaire du CDN retiré du message", "SECRET" not in message and "codeload.github.com" in message,
          message)


# ---------------------------------------------------------------------------

def main_test() -> int:
    print("Tests des dépôts GitHub privés")
    test_url_parsing()
    test_source_mode_and_environment()
    test_access_check()
    test_entry_points()
    test_source_endpoint()
    test_relay_gate()
    test_fetcher_contract()
    test_signed_url_masking()

    print(f"\n{'=' * 60}")
    print(f"{CHECKS - len(FAILURES)}/{CHECKS} vérifications réussies")
    if FAILURES:
        print("ÉCHECS :")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("Dépôts privés vérifiés : le jeton GitHub ne quitte jamais l'API.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_test())
