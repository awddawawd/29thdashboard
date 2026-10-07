"""Preuves d'isolation multi-tenant (correctif C3).

Vérifie qu'aucun secret du plan de contrôle (clé Supabase ``service_role``) ne
peut plus atteindre un conteneur locataire, et qu'un jeton de synchronisation
volé dans un conteneur ne donne accès qu'aux données de **son** locataire.

Exécution :

    .venv\\Scripts\\python.exe tests\\test_tenant_isolation.py
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import os
import pathlib
import shutil
import socketserver
import sys
import tempfile
import threading
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "End"))
os.chdir(ROOT / "End")

# Secret dédié pour des jetons déterministes et reproductibles.
os.environ["WAAS_SYNC_SECRET"] = "secret-de-test-isolation"

import sync_auth  # noqa: E402
import main  # noqa: E402

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


# ---------------------------------------------------------------------------
# 1. Jetons scopés par locataire
# ---------------------------------------------------------------------------

def test_sync_tokens() -> None:
    section("1. Jetons HMAC scopés par locataire")

    token_a = sync_auth.issue_tenant_token("boutique-a")
    token_b = sync_auth.issue_tenant_token("boutique-b")

    check("jeton valide -> tenant restitué", sync_auth.verify_tenant_token(token_a) == "boutique-a")
    check("jetons distincts par locataire", token_a != token_b)
    check("jeton de A ne désigne jamais B", sync_auth.verify_tenant_token(token_a) != "boutique-b")

    # Signature altérée
    tenant, _, signature = token_a.rpartition(".")
    tampered = f"{tenant}.{('A' if signature[0] != 'A' else 'B') + signature[1:]}"
    check("signature altérée rejetée", sync_auth.verify_tenant_token(tampered) is None)

    # Tentative de changement de locataire en réutilisant la signature de A
    check("réutilisation de signature pour un autre tenant rejetée",
          sync_auth.verify_tenant_token(f"boutique-b.{signature}") is None)

    # Forge avec un autre secret serveur
    os.environ["WAAS_SYNC_SECRET"] = "secret-attaquant"
    forged = sync_auth.issue_tenant_token("boutique-b")
    os.environ["WAAS_SYNC_SECRET"] = "secret-de-test-isolation"
    check("jeton forgé sans le secret serveur rejeté",
          sync_auth.verify_tenant_token(forged) is None)

    for bogus in ("", None, "sans-point", ".", "tenant.", ".signature", "a" * 400):
        check(f"jeton malformé rejeté ({bogus!r:.20})", sync_auth.verify_tenant_token(bogus) is None)


# ---------------------------------------------------------------------------
# 2. Environnement de déploiement d'un conteneur locataire
# ---------------------------------------------------------------------------

def test_tenant_environment() -> None:
    section("2. Environnement livré au conteneur locataire")

    environment = main.build_tenant_environment(
        tenant_id="boutique-a",
        repo_url="https://github.com/exemple/template.git",
        api_url="https://api.waas.example",
    )
    lowered = {k.upper() for k in environment}

    check("aucune variable SUPABASE_*", not any(k.startswith("SUPABASE") for k in lowered), str(lowered))
    check("clé service_role absente de l'environnement",
          "service_role" not in json.dumps(environment).lower()
          and main.db.SUPABASE_KEY not in json.dumps(environment))
    check("jeton scopé présent et valide",
          sync_auth.verify_tenant_token(environment.get("SYNC_TOKEN", "")) == "boutique-a")
    check("variables utiles conservées",
          {"TENANT_ID", "REPO_URL", "API_BASE_URL"} <= lowered, str(sorted(lowered)))

    # Le code source ne doit plus réinjecter la clé dans un conteneur
    source = (ROOT / "End" / "main.py").read_text(encoding="utf-8")
    check("plus d'injection de db.SUPABASE_KEY dans un conteneur",
          '"SUPABASE_KEY": db.SUPABASE_KEY' not in source)
    check("helper d'environnement utilisé par le déploiement",
          "build_tenant_environment(" in source and "repo_url=repo_url" in source)
    check("durcissement appliqué au déploiement",
          'cap_drop=["ALL"]' in source and 'security_opt=["no-new-privileges:true"]' in source
          and "read_only=True" in source and "mem_limit=" in source and "pids_limit=" in source)


# ---------------------------------------------------------------------------
# 2b. Les deux vues de l'API : conteneur (synchronisation) et navigateur
# ---------------------------------------------------------------------------

def test_deploy_environment_wiring() -> None:
    """``api_public_url`` sert les conteneurs ; le navigateur a sa propre URL.

    Régression : ``host.docker.internal`` ne résout pas depuis un poste de
    travail. Utilisée à la fois pour l'endpoint du micro-tracker (gravé dans le
    HTML des vitrines) et pour ``frame-ancestors``, elle rendait la collecte
    d'audience silencieusement muette et bloquait l'aperçu encadré du Studio.
    """
    section("2b. Environnement de déploiement : audience et aperçu côté navigateur")

    captured: dict[str, object] = {}

    class FakeContainer:
        def reload(self) -> None:
            pass

    class FakeContainers:
        def run(self, **kwargs: object) -> FakeContainer:
            captured.update(kwargs)
            return FakeContainer()

    class FakeNetworks:
        def get(self, name: str) -> object:
            raise main.NotFound(name)

        def create(self, name: str, **kwargs: object) -> None:
            return None

    settings = {
        "api_public_url": "http://host.docker.internal:8000",
        "browser_api_url": "http://localhost:8000",
        "docker_base_image": "waas-base-image:latest",
    }

    original_client, original_get = main.docker_client, main.db.get_setting
    main.docker_client = SimpleNamespace(containers=FakeContainers(), networks=FakeNetworks())
    main.db.get_setting = lambda key, default=None: settings.get(key, default if default is not None else "")
    try:
        main.provision_container("boutique-a", "https://github.com/exemple/template.git")
    finally:
        main.docker_client, main.db.get_setting = original_client, original_get

    environment = captured.get("environment") or {}
    check("synchronisation : URL joignable depuis le conteneur conservée",
          environment.get("API_BASE_URL") == "http://host.docker.internal:8000",
          str(environment.get("API_BASE_URL")))
    check("micro-tracker : URL joignable depuis le navigateur transmise",
          environment.get("BROWSER_API_URL") == "http://localhost:8000",
          str(environment.get("BROWSER_API_URL")))
    check("frame-ancestors : origine du Studio + alias de bouclage",
          environment.get("PORTAL_ORIGIN") == "http://localhost:8000 http://127.0.0.1:8000",
          str(environment.get("PORTAL_ORIGIN")))
    check("hôte des conteneurs toujours lié",
          captured.get("extra_hosts") == {"host.docker.internal": "host-gateway"},
          str(captured.get("extra_hosts")))


# ---------------------------------------------------------------------------
# 3. Endpoints de synchronisation côté API
# ---------------------------------------------------------------------------

def test_internal_endpoints() -> None:
    section("3. Endpoints internes de synchronisation")

    from fastapi.testclient import TestClient

    captured: dict[str, object] = {}

    def fake_get_tenant_content(tenant_id: str, file_path=None, strict: bool = False):
        captured["get_tenant"] = tenant_id
        return [{
            "id": "row-1", "tenant_id": tenant_id, "file_path": "index.html",
            "xpath": "/html/body/h1", "content_value": "Titre", "content_type": "text",
        }]

    def fake_upsert_tenant_content(tenant_id: str, records):
        captured["post_tenant"] = tenant_id
        captured["post_records"] = records
        return len(records)

    main.db.get_tenant_content = fake_get_tenant_content            # type: ignore[assignment]
    main.db.upsert_tenant_content = fake_upsert_tenant_content      # type: ignore[assignment]

    client = TestClient(main.app)          # pas de lifespan : aucune collecte réelle
    token_a = sync_auth.issue_tenant_token("boutique-a")
    token_b = sync_auth.issue_tenant_token("boutique-b")
    path = "/internal/sync/content"

    answer = client.get(path)
    check("lecture sans jeton refusée (401)", answer.status_code == 401, str(answer.status_code))

    answer = client.get(path, headers={"X-WAAS-Sync-Token": "boutique-a.faux"})
    check("jeton invalide refusé (401)", answer.status_code == 401)

    answer = client.get(path, headers={"X-WAAS-Sync-Token": token_a})
    check("lecture authentifiée (200)", answer.status_code == 200, answer.text[:120])
    check("le tenant provient du jeton", captured.get("get_tenant") == "boutique-a",
          str(captured.get("get_tenant")))
    record = (answer.json().get("records") or [{}])[0]
    check("projection minimale (ni id, ni tenant_id exposés)",
          set(record) == {"file_path", "xpath", "content_value"}, str(sorted(record)))

    # Tentative de ciblage d'un autre locataire via un paramètre de requête
    answer = client.get(f"{path}?tenant_id=boutique-b", headers={"X-WAAS-Sync-Token": token_a})
    check("paramètre tenant_id ignoré (pas d'IDOR)",
          answer.json().get("tenant_id") == "boutique-a" and captured.get("get_tenant") == "boutique-a")

    # Écriture : le client ne peut pas imposer un autre tenant
    payload = {"records": [{
        "file_path": "index.html", "xpath": "/html/body/h1",
        "content_type": "text", "content_value": "Pirate", "tenant_id": "boutique-b",
    }]}
    answer = client.post(path, headers={"X-WAAS-Sync-Token": token_a}, json=payload)
    check("écriture authentifiée (200)", answer.status_code == 200, answer.text[:120])
    check("tenant forcé côté serveur", captured.get("post_tenant") == "boutique-a",
          str(captured.get("post_tenant")))
    written = (captured.get("post_records") or [{}])[0]
    check("tenant_id injecté par le client ignoré", "tenant_id" not in written, str(sorted(written)))

    answer = client.post(path, headers={"X-WAAS-Sync-Token": token_b}, json=payload)
    check("le même envoi par B cible bien B", captured.get("post_tenant") == "boutique-b")

    answer = client.post(path, json=payload)
    check("écriture sans jeton refusée (401)", answer.status_code == 401)

    too_many = {"records": [{
        "file_path": "a.html", "xpath": "/p", "content_type": "text", "content_value": "x",
    }] * (main.MAX_SYNC_RECORDS + 1)}
    answer = client.post(path, headers={"X-WAAS-Sync-Token": token_a}, json=too_many)
    check("charge excessive refusée (422)", answer.status_code == 422, str(answer.status_code))

    spec = client.get("/openapi.json").json()
    check("endpoints internes absents de la documentation publique",
          path not in spec.get("paths", {}))


# ---------------------------------------------------------------------------
# 4. Moteur de synchronisation du conteneur (contrat HTTP réel)
# ---------------------------------------------------------------------------

class _StubApi(http.server.BaseHTTPRequestHandler):
    """API factice : protocole v2 (inventaire) et v1, contenu en mémoire."""

    records: list[dict] = []
    calls: list[tuple] = []

    def log_message(self, *args):  # silence
        return

    def _respond(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path != "/internal/sync/content":
            self._respond({"error": "not found"}, 404)
            return
        type(self).calls.append(("GET", self.headers.get("X-WAAS-Sync-Token")))
        type(self)._respond(self, {"tenant_id": "boutique-a", "total": len(type(self).records),
                                   "records": type(self).records})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).calls.append(("POST", self.headers.get("X-WAAS-Sync-Token"), payload, self.path,
                                 self.headers.get("User-Agent")))
        if self.path != "/internal/sync/inventory":
            type(self)._respond(self, {"tenant_id": "boutique-a", "upserted": len(payload.get("records", []))})
            return
        stored = {(r["file_path"], r["xpath"]): r for r in type(self).records}
        values, created = [], 0
        for slot in payload.get("slots", []):
            record = stored.get((payload["file_path"], slot["xpath"]))
            if record is None:
                type(self).records.append({"file_path": payload["file_path"], "xpath": slot["xpath"],
                                           "content_value": slot["source"]})
                created += 1
            elif record["content_value"] != slot["source"]:
                values.append({"xpath": slot["xpath"], "content_value": record["content_value"]})
        type(self)._respond(self, {"protocol": 2, "tenant_id": "boutique-a", "values": values,
                                   "stats": {"inserted": created}})


def _load_sync_engine(port: int, web_root: pathlib.Path, token: str):
    os.environ.update({
        "TENANT_ID": "boutique-a",
        "SYNC_TOKEN": token,
        "API_BASE_URL": f"http://127.0.0.1:{port}",
        "WEB_ROOT": str(web_root),
        # Copie d'origine du gabarit hors de la racine web (tmpfs dans le conteneur).
        "WAAS_PRISTINE_DIR": f"{web_root}-pristine",
        "SYNC_ATTEMPTS": "2",
        "SYNC_TIMEOUT": "5",
    })
    spec = importlib.util.spec_from_file_location(
        "waas_sync_engine_under_test", ROOT / "waas-base-image" / "sync_engine.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_sync_engine_contract() -> None:
    section("4. Moteur de synchronisation du conteneur (extraction + hydratation)")
    os.environ.pop("BROWSER_API_URL", None)          # repli testé explicitement plus bas

    with socketserver.TCPServer(("127.0.0.1", 0), _StubApi) as server:
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        token = sync_auth.issue_tenant_token("boutique-a")

        with tempfile.TemporaryDirectory() as tmp:
            web_root = pathlib.Path(tmp)
            page = web_root / "index.html"
            page.write_text(
                "<html><body><h1>Titre du template</h1>"
                '<img src="photo.png" alt="p">'
                "<p>Paragraphe</p></body></html>",
                encoding="utf-8",
            )

            # --- Premier lancement : extraction puis envoi ---
            _StubApi.records = []
            _StubApi.calls = []
            engine_source = (ROOT / "waas-base-image" / "sync_engine.py").read_text(encoding="utf-8")
            check("le moteur n'importe plus de client Supabase",
                  "import supabase" not in engine_source
                  and "create_client" not in engine_source
                  and "SUPABASE_KEY" not in engine_source)
            engine = _load_sync_engine(port, web_root, token)
            engine.run_sync()

            posts = [c for c in _StubApi.calls if c[0] == "POST"]
            check("échanges authentifiés par le jeton", posts and all(c[1] == token for c in _StubApi.calls),
                  str(posts[:1])[:160])
            check("en-tête User-Agent personnalisé (anti-blocage Cloudflare WAF)",
                  posts and all("Python-urllib" not in str(c[4]) and "WaaS-SyncEngine" in str(c[4]) for c in posts),
                  str([c[4] for c in posts]))
            check("premier lancement : inventaire des éléments envoyé (protocole v2)",
                  bool(posts) and posts[0][3] == "/internal/sync/inventory"
                  and len(posts[0][2].get("slots", [])) >= 3, str(posts[:1])[:160])
            if posts:
                sent = posts[0][2]["slots"]
                check("l'envoi ne porte pas de tenant_id (imposé par l'API)",
                      "tenant_id" not in posts[0][2] and all("tenant_id" not in r for r in sent))
                check("textes et images détectés",
                      {"text", "image"} <= {r.get("content_type") for r in sent},
                      str({r.get("content_type") for r in sent}))

            page.write_text(
                "<html><body><h1>Titre du template</h1>"
                '<img src="photo.png" alt="p">'
                "<p>Paragraphe</p></body></html>",
                encoding="utf-8",
            )

            # --- Second lancement (dépôt recloné) : hydratation depuis l'API ---
            for record in _StubApi.records:
                if record["xpath"] == "/html/body/h1":
                    record["content_value"] = "Titre client"
            indexed = len(_StubApi.records)
            _StubApi.calls = []
            engine = _load_sync_engine(port, web_root, token)
            engine.run_sync()

            hydrated = page.read_text(encoding="utf-8")
            check("contenu du client injecté dans le HTML", "Titre client" in hydrated)
            check("attributs d'édition ajoutés (annotations courtes)", 'data-waas="' in hydrated
                  and "data-waas-xpath" not in hydrated)
            check("tracker analytics injecté", "WAAS RUNTIME ANALYTICS" in hydrated)
            check("aucun élément ré-indexé au second lancement", len(_StubApi.records) == indexed,
                  f"{indexed} -> {len(_StubApi.records)}")
            check("aucune écriture v1 pendant l'hydratation",
                  not [c for c in _StubApi.calls if c[0] == "POST" and c[3] == "/internal/sync/content"])
            check("chemins d'API conformes au backend",
                  engine.SYNC_ENDPOINT == "/internal/sync/content"
                  and engine.INVENTORY_ENDPOINT in {getattr(r, "path", "") for r in main.app.routes},
                  engine.INVENTORY_ENDPOINT)

            # Le micro-tracker est appelé par le **navigateur du visiteur** : son
            # endpoint doit être l'URL « vue du navigateur », jamais celle des
            # conteneurs (host.docker.internal ne résout pas depuis un poste).
            check("tracker : repli sur API_BASE_URL sans BROWSER_API_URL",
                  engine._ANALYTICS_ENDPOINT == f"http://127.0.0.1:{port}/waas/analytics/hit/boutique-a",
                  engine._ANALYTICS_ENDPOINT)

            os.environ["BROWSER_API_URL"] = "http://localhost:8000"
            try:
                engine = _load_sync_engine(port, web_root, token)
                check("tracker : BROWSER_API_URL prime (endpoint joignable du navigateur)",
                      engine._ANALYTICS_ENDPOINT == "http://localhost:8000/waas/analytics/hit/boutique-a",
                      engine._ANALYTICS_ENDPOINT)
                check("la synchronisation du conteneur garde API_BASE_URL",
                      engine.API_BASE_URL == f"http://127.0.0.1:{port}", engine.API_BASE_URL)
            finally:
                os.environ.pop("BROWSER_API_URL", None)
                shutil.rmtree(f"{web_root}-pristine", ignore_errors=True)

        server.shutdown()


# ---------------------------------------------------------------------------

def main_test() -> int:
    print("Tests d'isolation multi-tenant (correctif C3)")
    test_sync_tokens()
    test_tenant_environment()
    test_deploy_environment_wiring()
    test_internal_endpoints()
    test_sync_engine_contract()

    print(f"\n{'=' * 60}")
    print(f"{CHECKS - len(FAILURES)}/{CHECKS} vérifications réussies")
    if FAILURES:
        print("ÉCHECS :")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("Isolation locataire vérifiée : aucun secret partagé n'atteint un conteneur.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_test())
