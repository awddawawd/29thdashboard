"""Éditeur visuel v2 — intégration : moteur du conteneur ↔ API ↔ base.

Le moteur réel (``waas-base-image/sync_engine.py``) synchronise de vraies pages
contre l'API réelle (``End/main.py``, servie par uvicorn), adossée à une base
Supabase simulée en mémoire qui applique la contrainte d'unicité
(tenant, page, xpath) et les colonnes facultatives de la migration 002.

Couvre : indexation au premier démarrage, isolation des pages (un même XPath sur
deux pages), modifications du Studio avec contrôle de conflit, publication à
chaud idempotente, évolution du gabarit (déplacements, retouches, orphelins),
mode dégradé sans migration, mise à niveau d'enregistrements historiques, repli
sur le protocole v1 et endpoints HTTP (droits, validation, publication).

Exécution :

    .venv\\Scripts\\python.exe tests\\test_editor_sync.py
"""

from __future__ import annotations

import contextlib
import http.server
import importlib.util
import io
import json
import os
import pathlib
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "End"))
os.chdir(ROOT / "End")
os.environ["WAAS_SYNC_SECRET"] = "secret-de-test-editeur"

import jwt  # noqa: E402
import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import content_sync  # noqa: E402
import main  # noqa: E402
import sync_auth  # noqa: E402

db = main.db
FAILURES: list[str] = []
CHECKS = 0
TENANT = "boutique-a"
BASE_COLUMNS = ("id", "tenant_id", "file_path", "xpath", "content_type", "content_value")
EDITOR_COLUMNS = ("source_value", "updated_at", "updated_by")


def check(name: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(f"{name} {detail}")
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f" -> {detail}" if detail and not condition else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
# Base simulée (sous-ensemble de postgrest-py utilisé par db.py)
# ---------------------------------------------------------------------------

class FakeApiError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code} {message}")
        self.code = code


class FakeStore:
    def __init__(self, with_editor_columns: bool = True) -> None:
        self.columns = set(BASE_COLUMNS) | (set(EDITOR_COLUMNS) if with_editor_columns else set())
        self.rows: list[dict[str, Any]] = []
        self.next_id = 1
        self.probe_error = ""                  # code d'erreur simulé sur un sondage de colonne

    def enable_editor_columns(self) -> None:
        self.columns |= set(EDITOR_COLUMNS)
        for row in self.rows:
            for column in EDITOR_COLUMNS:
                row.setdefault(column, None)

    def tenant_rows(self, tenant: str = TENANT) -> list[dict[str, Any]]:
        return [row for row in self.rows if row["tenant_id"] == tenant]

    def value(self, file_path: str, xpath: str) -> Any:
        row = next((r for r in self.rows if r["file_path"] == file_path and r["xpath"] == xpath), None)
        return row and row["content_value"]


class FakeQuery:
    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.op = "select"
        self.payload: Any = None
        self.filters: list[Any] = []
        self.window: tuple[int, int] | None = None
        self.ignore_duplicates = False
        self.columns = "*"

    def _require(self, names: Any, code: str) -> None:
        missing = [name for name in names if name not in self.store.columns]
        if missing:
            raise FakeApiError(code, f"colonne inconnue : {missing}")

    def select(self, columns: str = "*", count: Any = None) -> "FakeQuery":
        if columns != "*":
            if self.store.probe_error:
                raise FakeApiError(self.store.probe_error, "incident réseau simulé")
            self._require([c.strip() for c in columns.split(",")], "42703")
        self.op, self.columns = "select", columns
        return self

    def update(self, fields: dict[str, Any]) -> "FakeQuery":
        self._require(fields, "PGRST204")
        self.op, self.payload = "update", dict(fields)
        return self

    def delete(self) -> "FakeQuery":
        self.op = "delete"
        return self

    def upsert(self, rows: list[dict[str, Any]], on_conflict: str = "", ignore_duplicates: bool = False,
               **_: Any) -> "FakeQuery":
        for row in rows:
            self._require(row, "PGRST204")
        self.op, self.payload, self.ignore_duplicates = "upsert", [dict(r) for r in rows], ignore_duplicates
        return self

    def eq(self, column: str, value: Any) -> "FakeQuery":
        self.filters.append(lambda row: row.get(column) == value)
        return self

    def is_(self, column: str, _value: str) -> "FakeQuery":
        self.filters.append(lambda row: row.get(column) is None)
        return self

    def order(self, *_: Any, **__: Any) -> "FakeQuery":
        return self

    def limit(self, _n: int) -> "FakeQuery":
        return self

    def range(self, start: int, end: int) -> "FakeQuery":
        self.window = (start, end)
        return self

    @staticmethod
    def _key(row: dict[str, Any]) -> tuple:
        return row["tenant_id"], row["file_path"], row["xpath"]

    def execute(self) -> SimpleNamespace:
        matched = [row for row in self.store.rows if all(test(row) for test in self.filters)]
        if self.op == "select":
            matched.sort(key=lambda row: row["id"])
            if self.window:
                matched = matched[self.window[0]:self.window[1] + 1]
            return SimpleNamespace(data=[dict(row) for row in matched], count=len(matched))
        if self.op == "delete":
            self.store.rows = [row for row in self.store.rows if row not in matched]
            return SimpleNamespace(data=[dict(row) for row in matched], count=None)
        if self.op == "update":
            others = {self._key(row) for row in self.store.rows if row not in matched}
            for row in matched:
                if self._key({**row, **self.payload}) in others:
                    raise FakeApiError("23505", "clé dupliquée")
            for row in matched:
                row.update(self.payload)
            return SimpleNamespace(data=[dict(row) for row in matched], count=None)
        written = []
        index = {self._key(row): row for row in self.store.rows}
        for incoming in self.payload:
            existing = index.get(self._key(incoming))
            if existing is not None:
                if not self.ignore_duplicates:
                    existing.update(incoming)
                    written.append(dict(existing))
                continue
            row = {column: None for column in self.store.columns}
            row.update(incoming, id=self.store.next_id)
            self.store.next_id += 1
            self.store.rows.append(row)
            index[self._key(row)] = row
            written.append(dict(row))
        return SimpleNamespace(data=written, count=None)


class FakeSupabase:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    def table(self, name: str) -> FakeQuery:
        assert name == db.TABLE_CONTENT, name
        return FakeQuery(self.store)


def use_store(store: FakeStore) -> FakeStore:
    db.supabase = FakeSupabase(store)
    db._CONTENT_COLUMNS_PRESENT.clear()
    with db._MISSING_LOCK:
        db._MISSING.clear()
    return store


# ---------------------------------------------------------------------------
# Vitrine de test (deux pages au même squelette : même XPath, pages distinctes)
# ---------------------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8"><title>{title}</title></head>
<body>
{promo}<section class="hero">
  <h1>{heading} <em>vivant</em> du quartier</h1>
  <a class="btn" href="/contact.html"><svg viewBox="0 0 8 8"></svg> Nous écrire</a>
  <img src="hero.jpg" alt="Vitrine">
  {extra}
</section>
<footer><p>{footer}</p></footer>
</body></html>
"""


def write_site(web_root: pathlib.Path, *, promo: str = "", footer: str = "© 2026 Boulangerie Dupont",
               extra: str = "<p>Horaires : 6h-20h</p>") -> None:
    shutil.rmtree(web_root, ignore_errors=True)
    (web_root / "about").mkdir(parents=True)
    (web_root / "index.html").write_text(PAGE.format(title="Accueil", heading="Le pain", promo=promo,
                                                     footer=footer, extra=extra), encoding="utf-8")
    (web_root / "about" / "index.html").write_text(
        PAGE.format(title="À propos", heading="Notre histoire", promo=promo, footer=footer, extra=extra),
        encoding="utf-8")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def api_server():
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning",
                                           lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def load_engine(api_url: str, web_root: pathlib.Path, pristine: pathlib.Path) -> Any:
    os.environ.update({
        "TENANT_ID": TENANT, "SYNC_TOKEN": sync_auth.issue_tenant_token(TENANT), "API_BASE_URL": api_url,
        "BROWSER_API_URL": "http://localhost:8000", "WEB_ROOT": str(web_root),
        "WAAS_PRISTINE_DIR": str(pristine), "SYNC_ATTEMPTS": "2", "SYNC_TIMEOUT": "10",
        "WAAS_BRIDGE_SOURCE": str(ROOT / "waas-base-image" / "editor"),
    })
    spec = importlib.util.spec_from_file_location("waas_sync_engine_v2", ROOT / "waas-base-image" / "sync_engine.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run_engine(engine: Any, rehydrate: bool = False) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = engine.main(["--rehydrate"] if rehydrate else [])
    output = buffer.getvalue()
    if code != 0:
        print(output)
    return output if code == 0 else f"EXIT {code}\n{output}"


def key_of(file_path: str, source: str, store: FakeStore) -> str:
    return next(row["xpath"] for row in store.tenant_rows()
                if row["file_path"] == file_path and row["content_value"] == source)


# ---------------------------------------------------------------------------
# 1. Cycle complet avec la migration 002
# ---------------------------------------------------------------------------

def test_full_cycle(api_url: str, workdir: pathlib.Path) -> None:
    section("1. Premier démarrage, Studio, publication à chaud")
    store = use_store(FakeStore())
    web_root, pristine = workdir / "site", workdir / "pristine"
    write_site(web_root)
    engine = load_engine(api_url, web_root, pristine)

    output = run_engine(engine)
    check("démarrage réussi (protocole v2)", "protocole v2" in output, output[-300:])
    rows = store.tenant_rows()
    by_file = {path: [r for r in rows if r["file_path"] == path] for path in ("index.html", "about/index.html")}
    check("les deux pages sont indexées séparément", all(len(items) >= 8 for items in by_file.values()),
          str({k: len(v) for k, v in by_file.items()}))
    check("ancre enregistrée (texte d'origine)", all(r["source_value"] == r["content_value"] for r in rows))
    check("texte après l'icône indexé", any(r["content_value"] == "Nous écrire" for r in by_file["index.html"]))
    index_html = (web_root / "index.html").read_text(encoding="utf-8")
    check("page publique annotée, DOCTYPE conservé",
          index_html.startswith("<!DOCTYPE html>") and 'data-waas="' in index_html)
    bridge = web_root / "__waas" / "editor-bridge.js"
    bundle = bridge.read_text(encoding="utf-8") if bridge.is_file() else ""
    check("pont d'édition publié (parties assemblées dans une seule portée)",
          bundle.startswith("/*! Pont d'édition") and bundle.rstrip().endswith("})();")
          and all(f"/* --- {part} --- */" in bundle for part in ("10-dom.js", "20-overlay.js", "30-bridge.js")))
    node = shutil.which("node")
    if node:
        syntax = subprocess.run([node, "--check", str(bridge)], capture_output=True, text=True)
        check("pont assemblé : JavaScript valide", syntax.returncode == 0, syntax.stderr[-300:])
    check("carte d'édition publiée", (web_root / "__waas" / "map" / "about" / "index.html.json").is_file())
    check("gabarit d'origine conservé à part", (pristine / "about" / "index.html").is_file())

    h1 = key_of("index.html", "Le pain", store)
    tail = key_of("index.html", "du quartier", store)
    image = key_of("index.html", "hero.jpg", store)
    footer = key_of("index.html", "© 2026 Boulangerie Dupont", store)
    results = db.update_content_values(TENANT, [
        {"xpath": h1, "file_path": "index.html", "new_value": "Le levain", "expected_value": "Le pain"},
        {"xpath": tail, "file_path": "index.html", "new_value": "de la ville", "expected_value": "du quartier"},
        {"xpath": image, "file_path": "index.html", "new_value": "https://cdn.example.com/h.webp"},
        {"xpath": footer, "file_path": "index.html", "new_value": "© Dupont & fils"},
        {"xpath": h1, "file_path": "index.html", "new_value": "Autre", "expected_value": "Le pain"},
        {"xpath": image, "file_path": "index.html", "new_value": "javascript:alert(1)"},
        {"xpath": "/html/body/absent", "file_path": "index.html", "new_value": "x"},
    ], author="agence")
    statuses = [result["status"] for result in results]
    check("statuts par modification",
          statuses == ["updated", "updated", "updated", "updated", "conflict", "invalid", "not_found"], str(statuses))
    check("conflit : valeur actuelle renvoyée", results[4].get("current_value") == "Le levain")
    check("audit : auteur enregistré",
          next(r for r in store.rows if r["xpath"] == h1 and r["file_path"] == "index.html")["updated_by"] == "agence")
    check("même XPath sur l'autre page intact", store.value("about/index.html", h1) == "Notre histoire")

    output = run_engine(engine, rehydrate=True)
    check("publication à chaud signalée à l'API", "WAAS_REHYDRATE_OK" in output, output[-200:])
    published = (web_root / "index.html").read_text(encoding="utf-8")
    heading = published[published.find("<h1"):published.find("</h1>") + 5]
    check("titre publié sans duplication", "Le levain <em" in heading and heading.count("vivant") == 1
          and "de la ville</h1>" in heading, heading)
    check("image publiée", 'src="https://cdn.example.com/h.webp"' in published)
    check("autre page inchangée", "Notre histoire <em" in (web_root / "about" / "index.html").read_text("utf-8"))
    run_engine(engine, rehydrate=True)
    check("publication à chaud idempotente", (web_root / "index.html").read_text("utf-8") == published)
    data = json.loads((web_root / "__waas" / "map" / "index.html.json").read_text("utf-8"))
    check("carte à jour", any(entry["x"] == "Le levain" for entry in data["slots"]))

    section("2. Le gabarit évolue (nouveau commit, redémarrage)")
    write_site(web_root, promo='<section class="promo"><h2>Offre du mois</h2></section>\n',
               footer="© 2027 Boulangerie Dupont", extra="")
    output = run_engine(engine)
    check("redémarrage réussi", "Prêt pour le démarrage" in output, output[-300:])
    published = (web_root / "index.html").read_text(encoding="utf-8")
    heading = published[published.find("<h1"):published.find("</h1>") + 5]
    check("section insérée avant : le titre du client suit son élément", "Le levain <em" in heading, heading)
    new_h1 = key_of("index.html", "Le levain", store)
    check("clé déplacée en base", new_h1 == "/html/body/section[2]/h1", new_h1)
    check("nouvel élément indexé", any(r["content_value"] == "Offre du mois" for r in store.tenant_rows()))
    check("pied de page retouché : la version du client est conservée",
          "© Dupont &amp; fils" in published or "© Dupont & fils" in published)
    footer_row = next(r for r in store.tenant_rows() if r["file_path"] == "index.html"
                      and r["content_value"] == "© Dupont & fils")
    check("pied de page : ancre mise à jour", footer_row["source_value"] == "© 2027 Boulangerie Dupont")
    about = [r for r in store.tenant_rows() if r["file_path"] == "about/index.html"]
    check("élément supprimé jamais modifié : effacé", not any(r["content_value"] == "Horaires : 6h-20h" for r in about))
    check("aucun enregistrement en transit oublié",
          not any(r["xpath"].startswith(content_sync.MOVING_PREFIX) for r in store.rows))


def test_degraded_and_upgrade(api_url: str, workdir: pathlib.Path) -> None:
    section("3. Sans migration 002, puis mise à niveau")
    store = use_store(FakeStore(with_editor_columns=False))
    web_root, pristine = workdir / "site-legacy", workdir / "pristine-legacy"
    write_site(web_root)
    engine = load_engine(api_url, web_root, pristine)
    output = run_engine(engine)
    check("démarrage sans migration", "Prêt pour le démarrage" in output, output[-300:])
    check("aucune colonne facultative écrite", all("source_value" not in row for row in store.rows))
    h1 = key_of("index.html", "Le pain", store)
    result = db.update_content_values(TENANT, [{"xpath": h1, "file_path": "index.html", "new_value": "Le levain"}],
                                      author="client")[0]
    check("modification sans colonnes d'audit", result["status"] == "updated", str(result))
    run_engine(engine, rehydrate=True)
    check("publication sans migration", "Le levain <em" in (web_root / "index.html").read_text("utf-8"))

    store.enable_editor_columns()
    use_store(store)
    write_site(web_root)                       # redémarrage : le dépôt est recloné
    run_engine(engine)
    rows = store.tenant_rows()
    check("mise à niveau : ancres complétées", all(r["source_value"] is not None for r in rows),
          str([r["xpath"] for r in rows if r["source_value"] is None]))
    check("mise à niveau : personnalisation conservée",
          store.value("index.html", h1) == "Le levain"
          and next(r for r in rows if r["xpath"] == h1 and r["file_path"] == "index.html")["source_value"] == "Le pain")

    run_engine(engine)                         # relance sans reclonage (pages déjà hydratées)
    row = next(r for r in store.tenant_rows() if r["xpath"] == h1 and r["file_path"] == "index.html")
    check("relance sans reclonage : le gabarit d'origine fait toujours foi",
          row["source_value"] == "Le pain" and "Le levain <em" in (web_root / "index.html").read_text("utf-8"),
          str(row))


# ---------------------------------------------------------------------------
# 4. Repli sur une API antérieure (protocole v1)
# ---------------------------------------------------------------------------

class LegacyApi(http.server.BaseHTTPRequestHandler):
    records: list[dict[str, Any]] = []
    posts: list[dict[str, Any]] = []

    def log_message(self, *args: Any) -> None:
        return

    def _send(self, payload: dict[str, Any], code: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._send({"records": type(self).records})

    def do_POST(self) -> None:  # noqa: N802
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path != "/internal/sync/content":
            self._send({"detail": "Not Found"}, 404)
            return
        type(self).posts.append(payload)
        self._send({"upserted": len(payload.get("records", []))})


def test_legacy_api(workdir: pathlib.Path) -> None:
    section("4. API antérieure : repli sur le protocole v1")
    web_root, pristine = workdir / "site-v1", workdir / "pristine-v1"
    write_site(web_root)
    LegacyApi.records = [{"file_path": "index.html", "xpath": "/html/body/section/h1", "content_value": "Le levain"}]
    LegacyApi.posts = []
    with socketserver.TCPServer(("127.0.0.1", 0), LegacyApi) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        engine = load_engine(f"http://127.0.0.1:{server.server_address[1]}", web_root, pristine)
        output = run_engine(engine)
        server.shutdown()
    check("repli annoncé", "protocole v1" in output, output[-300:])
    pushed = [r for payload in LegacyApi.posts for r in payload.get("records", [])]
    check("seuls les éléments nouveaux sont envoyés",
          pushed and all(r["xpath"] != "/html/body/section/h1" or r["file_path"] != "index.html" for r in pushed))
    check("contenu du client appliqué", "Le levain <em" in (web_root / "index.html").read_text("utf-8"))


# ---------------------------------------------------------------------------
# 5. Endpoints HTTP
# ---------------------------------------------------------------------------

class FakeContainer:
    def __init__(self, exit_code: int, output: bytes) -> None:
        self.status = "running"
        self.exec_result = SimpleNamespace(exit_code=exit_code, output=output)
        self.restarted = 0
        self.commands: list[Any] = []

    def exec_run(self, command: Any, **_: Any) -> SimpleNamespace:
        self.commands.append(command)
        return self.exec_result

    def restart(self) -> None:
        self.restarted += 1


def test_endpoints() -> None:
    section("5. Endpoints : droits, validation, résultats, publication")
    store = use_store(FakeStore())
    original = (main.docker_client, db.get_user_by_username, db.get_site_by_id)
    db.get_user_by_username = lambda username: {"id": "admin-1", "username": username, "role": "admin"}
    db.get_site_by_id = lambda site_id: {"site_id": site_id, "client_id": "client-1"}
    container = FakeContainer(0, b"[SyncEngine] ...\nWAAS_REHYDRATE_OK files=1 applied=1\n")
    main.docker_client = SimpleNamespace(containers=SimpleNamespace(get=lambda name: container))
    client = TestClient(main.app)
    token = sync_auth.issue_tenant_token(TENANT)
    bearer = {"Authorization": "Bearer " + jwt.encode({"sub": "agence", "role": "admin"}, main.SECRET_KEY,
                                                       algorithm=main.ALGORITHM)}
    try:
        inventory = {"file_path": "index.html", "slots": [
            {"xpath": "/html/body/h1", "content_type": "text", "source": "Titre"},
            {"xpath": "/html/body/img", "content_type": "image", "source": "a.jpg"},
        ]}
        answer = client.post("/internal/sync/inventory", json=inventory)
        check("inventaire sans jeton refusé (401)", answer.status_code == 401, str(answer.status_code))
        answer = client.post("/internal/sync/inventory", json=inventory, headers={"X-WAAS-Sync-Token": token})
        body = answer.json()
        check("inventaire : protocole v2", answer.status_code == 200 and body.get("protocol") == 2, answer.text[:200])
        check("inventaire : tenant imposé par le jeton",
              {row["tenant_id"] for row in store.rows} == {TENANT} and body["tenant_id"] == TENANT)
        for bad in ("../secret.html", "/abs.html", "a//b.html", "a\\b.html"):
            answer = client.post("/internal/sync/inventory", json={**inventory, "file_path": bad},
                                 headers={"X-WAAS-Sync-Token": token})
            check(f"chemin de page refusé : {bad!r}", answer.status_code == 422, str(answer.status_code))
        answer = client.post("/internal/sync/inventory", headers={"X-WAAS-Sync-Token": token}, json={
            "file_path": "x.html", "slots": [{"xpath": f"/p[{i}]", "source": "x"} for i in range(5001)]})
        check("inventaire démesuré refusé (422)", answer.status_code == 422, str(answer.status_code))
        check("inventaire absent de la documentation publique",
              "/internal/sync/inventory" not in client.get("/openapi.json").json().get("paths", {}))

        answer = client.get(f"/api/v1/sites/{TENANT}/content", headers=bearer)
        check("lecture du contenu (agence)", answer.status_code == 200 and answer.json()["count"] == 2,
              answer.text[:200])
        check("migration détectée", answer.json().get("anchors") is True)

        changes = {"changes": [
            {"xpath": "/html/body/h1", "file_path": "index.html", "new_value": "Bonjour", "expected_value": "Titre"},
            {"xpath": "/html/body/img", "file_path": "index.html", "new_value": "javascript:alert(1)"},
        ]}
        answer = client.put(f"/api/v1/sites/{TENANT}/content/batch", json=changes, headers=bearer)
        body = answer.json()
        check("lot : statuts détaillés", [r["status"] for r in body.get("results", [])] == ["updated", "invalid"],
              answer.text[:300])
        check("lot : publication à chaud", body.get("published") is True and body.get("publish_method") == "hot"
              and container.restarted == 0, str(body))
        check("publication : moteur v2 exigé, sinon repli",
              "waas_content.py" in container.commands[-1][-1] and "--rehydrate" in container.commands[-1][-1])

        answer = client.put(f"/api/v1/sites/{TENANT}/content/batch", json=changes, headers=bearer)
        check("même lot rejoué : « inchangé » (déjà enregistré), et republié",
              answer.json()["results"][0]["status"] == "unchanged" and answer.json().get("published") is True,
              answer.text[:200])
        stale = {"changes": [{"xpath": "/html/body/h1", "file_path": "index.html", "new_value": "Autre",
                              "expected_value": "Titre"}]}
        answer = client.put(f"/api/v1/sites/{TENANT}/content/batch", json=stale, headers=bearer)
        check("valeur attendue périmée : conflit détecté", answer.json()["results"][0]["status"] == "conflict"
              and answer.json()["results"][0].get("current_value") == "Bonjour", answer.text[:200])

        container.exec_result = SimpleNamespace(exit_code=main.HOT_PUBLISH_UNSUPPORTED, output=b"")
        answer = client.put(f"/api/v1/sites/{TENANT}/content/batch", headers=bearer, json={"changes": [
            {"xpath": "/html/body/h1", "file_path": "index.html", "new_value": "Salut"}]})
        check("image antérieure au moteur v2 : repli par redémarrage",
              answer.json().get("publish_method") == "restart" and container.restarted == 1, answer.text[:200])

        answer = client.patch(f"/api/v1/sites/{TENANT}/content", headers=bearer,
                              json={"xpath": "/html/body/img", "file_path": "index.html", "new_value": "data:x"})
        check("modification unitaire invalide : 422 explicite", answer.status_code == 422, answer.text[:200])

        orphan = "orphan:20260101T000000Z:/html/body/h2"
        store.rows.append({"id": 999, "tenant_id": TENANT, "file_path": "index.html", "xpath": orphan,
                           "content_type": "text", "content_value": "Ancien", "source_value": "x",
                           "updated_at": None, "updated_by": None})
        answer = client.delete(f"/api/v1/sites/{TENANT}/content", headers=bearer,
                               params={"file_path": "index.html", "xpath": "/html/body/h1"})
        check("élément actif : suppression refusée (422)", answer.status_code == 422, str(answer.status_code))
        answer = client.delete(f"/api/v1/sites/{TENANT}/content", headers=bearer,
                               params={"file_path": "index.html", "xpath": orphan})
        check("contenu détaché supprimé", answer.status_code == 200 and not any(r["xpath"] == orphan for r in store.rows))

        answer = client.put(f"/api/v1/sites/{TENANT}/content/batch", json=changes)
        check("lot sans authentification refusé (401)", answer.status_code == 401, str(answer.status_code))
        too_long = {"changes": [{"xpath": "/p", "file_path": "index.html", "new_value": "x" * 5001}]}
        answer = client.put(f"/api/v1/sites/{TENANT}/content/batch", json=too_long, headers=bearer)
        check("valeur démesurée refusée (422)", answer.status_code == 422, str(answer.status_code))
    finally:
        main.docker_client, db.get_user_by_username, db.get_site_by_id = original


# ---------------------------------------------------------------------------
# 6. Non-régressions issues de la revue de code
# ---------------------------------------------------------------------------

def test_review_regressions() -> None:
    section("6. Non-régressions (revue de code)")
    store = use_store(FakeStore())
    slots = [content_sync.InventorySlot("/h1", "text", "Titre"), content_sync.InventorySlot("/p", "text", "Texte")]
    db.apply_content_plan(TENANT, "index.html", content_sync.reconcile(slots, [], True), True)

    sent = {"xpath": "/h1", "file_path": "index.html", "new_value": "Bonjour", "expected_value": "Titre"}
    first = db.update_content_values(TENANT, [sent])[0]["status"]
    again = db.update_content_values(TENANT, [sent])[0]["status"]
    check("lot rejoué (réponse perdue) : « inchangé », jamais « conflit »",
          (first, again) == ("updated", "unchanged"), f"{first} {again}")

    row = next(r for r in store.rows if r["xpath"] == "/h1")
    stale = dict(row)
    row.update(content_value="Écrit par Camille", updated_at="2026-10-02T21:00:00+00:00")
    original_rows = db._content_rows
    db._content_rows = lambda tenant_id, file_path=None: [dict(stale)]
    try:
        result = db.update_content_values(TENANT, [{"xpath": "/h1", "file_path": "index.html",
                                                    "new_value": "Moi", "expected_value": stale["content_value"]}])[0]
    finally:
        db._content_rows = original_rows
    check("écriture gardée : un enregistrement concurrent devient un conflit",
          result["status"] == "conflict" and result.get("current_value") == "Écrit par Camille"
          and row["content_value"] == "Écrit par Camille", str(result))

    # Réconciliation rejouée (requête renvoyée après un délai dépassé) : aucun effet croisé.
    store = use_store(FakeStore())
    base = [content_sync.InventorySlot("/a", "text", "A"), content_sync.InventorySlot("/b", "text", "B")]
    db.apply_content_plan(TENANT, "index.html", content_sync.reconcile(base, [], True), True)
    for row in store.rows:
        row["content_value"] = {"/a": "A perso", "/b": "B perso"}[row["xpath"]]
    shifted = [content_sync.InventorySlot("/a", "text", "B"), content_sync.InventorySlot("/c", "text", "Nouveau")]
    records = [content_sync.record_from_row(row) for row in store.tenant_rows()]
    plan = content_sync.reconcile(shifted, records, True)
    db.apply_content_plan(TENANT, "index.html", plan, True)
    db.apply_content_plan(TENANT, "index.html", plan, True)
    rows = {row["xpath"]: row["content_value"] for row in store.tenant_rows()}
    orphans = [key for key in rows if key.startswith(content_sync.ORPHAN_PREFIX)]
    check("plan appliqué deux fois : la ligne déplacée n'est pas détachée à son tour",
          rows.get("/a") == "B perso" and len(orphans) == 1 and rows[orphans[0]] == "A perso", str(rows))

    store = use_store(FakeStore())
    original = (main.docker_client, db.get_user_by_username, db.get_site_by_id)
    container = FakeContainer(1, b"[SyncEngine - ERREUR] API de synchronisation injoignable")
    main.docker_client = SimpleNamespace(containers=SimpleNamespace(get=lambda name: container))
    try:
        outcome = main.publish_site_content(TENANT)
        check("réhydratation en échec : pas de redémarrage, la vitrine reste en ligne",
              outcome["published"] is False and container.restarted == 0 and outcome["detail"], str(outcome))
        container.exec_result = SimpleNamespace(exit_code=main.HOT_PUBLISH_UNSUPPORTED, output=b"")
        outcome = main.publish_site_content(TENANT)
        check("image antérieure : redémarrage", outcome["method"] == "restart" and container.restarted == 1)

        def refuse_exec(*_: Any, **__: Any) -> Any:
            raise main.APIError("exec interdit par le proxy de socket")
        container.exec_run = refuse_exec
        outcome = main.publish_site_content(TENANT)
        check("exec refusé (proxy de socket) : redémarrage", outcome["method"] == "restart" and container.restarted == 2)
    finally:
        main.docker_client, db.get_user_by_username, db.get_site_by_id = original

    client = TestClient(main.app)
    token = sync_auth.issue_tenant_token(TENANT)
    answer = client.post("/internal/sync/content", headers={"X-WAAS-Sync-Token": token}, json={"records": [
        {"file_path": "/evil.example/x.html", "xpath": "/p", "content_value": "x"},
        {"file_path": "../x.html", "xpath": "/p", "content_value": "x"},
        {"file_path": "ok.html", "xpath": "/p", "content_value": "x"},
    ]})
    check("protocole v1 : chemins de page hostiles ignorés, les autres indexés",
          answer.status_code == 200 and {row["file_path"] for row in store.rows} == {"ok.html"},
          f"{answer.status_code} {[row['file_path'] for row in store.rows]}")

    store.probe_error = "XX000"
    db._CONTENT_COLUMNS_PRESENT.clear()
    answer = client.post("/internal/sync/inventory", headers={"X-WAAS-Sync-Token": token},
                         json={"file_path": "index.html", "slots": [{"xpath": "/h1", "source": "Titre"}]})
    check("sondage des ancres en échec : 502 (jamais de publication par clé seule)",
          answer.status_code == 502, str(answer.status_code))
    store.probe_error = ""


# ---------------------------------------------------------------------------

def main_test() -> int:
    print("Éditeur visuel v2 : intégration moteur ↔ API ↔ base")
    with tempfile.TemporaryDirectory() as tmp, api_server() as api_url:
        workdir = pathlib.Path(tmp)
        test_full_cycle(api_url, workdir)
        test_degraded_and_upgrade(api_url, workdir)
        test_legacy_api(workdir)
    test_endpoints()
    test_review_regressions()

    print(f"\n{'=' * 60}")
    print(f"{CHECKS - len(FAILURES)}/{CHECKS} vérifications réussies")
    if FAILURES:
        print("ÉCHECS :")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("Chaîne d'édition vérifiée de bout en bout.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_test())
