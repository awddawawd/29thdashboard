"""Banc d'essai du Studio — développement local, sans Supabase ni Docker.

Démarre :

* l'API réelle (``End/main.py``) sur http://localhost:8765, adossée à la base
  simulée des tests (mémoire) et à un faux Docker dont « exec » publie à chaud
  avec le vrai moteur du conteneur (``waas-base-image/sync_engine.py``) ;
* une vitrine de démonstration de deux pages sur http://localhost:8766 (autre
  origine, comme en production), hydratée par ce même moteur.

Usage :

    .venv\\Scripts\\python.exe tests\\studio_harness.py [port_api] [port_vitrine]

puis ouvrir http://localhost:8765/harness (session « agence » ouverte d'office).
Ce n'est pas une suite de tests : rien n'est vérifié automatiquement.
"""

from __future__ import annotations

import contextlib
import http.server
import io
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import test_editor_sync as kit  # noqa: E402  (base simulée, chargeur du moteur)

import jwt  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import APIRouter  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402

main, db = kit.main, kit.db
API_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
SITE_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8766
SITE_ID = kit.TENANT
SITE_NAME = "Boulangerie Dupont"

STYLE = """
:root { --ink:#1d1a16; --muted:#6d6459; --cream:#f7f1e8; --crust:#b5622f; }
* { box-sizing: border-box; }
body { margin:0; font-family: Georgia, 'Times New Roman', serif; color:var(--ink); background:var(--cream); }
header { display:flex; justify-content:space-between; align-items:center; padding:20px 48px; }
nav { display:flex; gap:20px; align-items:center; }
nav a { color:var(--ink); text-decoration:none; font-family:system-ui, sans-serif; font-size:14px; }
.btn { display:inline-flex; align-items:center; gap:8px; padding:12px 20px; border:0; border-radius:999px;
  background:var(--crust); color:#fff; font:600 14px system-ui, sans-serif; cursor:pointer; text-decoration:none; }
.btn svg { width:16px; height:16px; }
.hero { display:grid; grid-template-columns:1.1fr 1fr; gap:48px; align-items:center; padding:48px; }
.hero h1 { margin:0 0 16px; font-size:clamp(40px, 5vw, 72px); line-height:1; letter-spacing:-0.03em; }
.hero h1 .accent { color:var(--crust); font-style:italic; }
.hero p { color:var(--muted); font-size:18px; line-height:1.6; }
.media { position:relative; border-radius:24px; overflow:hidden; aspect-ratio:4/3; }
.media img { width:100%; height:100%; object-fit:cover; display:block; }
.media::after { content:""; position:absolute; inset:0; background:linear-gradient(transparent 55%, rgba(0,0,0,.35)); }
.media .badge { position:absolute; left:20px; bottom:20px; z-index:1; color:#fff; font:600 14px system-ui, sans-serif; }
.stats { display:flex; gap:48px; padding:0 48px 48px; }
.stat strong { display:block; font-size:44px; }
.stat span { color:var(--muted); font-family:system-ui, sans-serif; }
.menu-panel { display:none; position:absolute; right:48px; top:64px; background:#fff; padding:16px; border-radius:12px; box-shadow:0 10px 30px rgba(0,0,0,.12); }
.menu-panel.is-open { display:block; }
table { margin:0 48px 48px; border-collapse:collapse; font-family:system-ui, sans-serif; }
td { padding:8px 16px; border-bottom:1px solid #e6dccd; }
form { display:flex; gap:12px; padding:0 48px 48px; }
input { padding:12px 16px; border-radius:999px; border:1px solid #d9cdbb; font:inherit; }
footer { padding:32px 48px; border-top:1px solid #e6dccd; color:var(--muted); font-family:system-ui, sans-serif; }
"""

SCRIPT = """
document.querySelector('[data-menu]').addEventListener('click', function () {
  document.querySelector('.menu-panel').classList.toggle('is-open');
});
document.querySelector('form').addEventListener('submit', function (event) {
  event.preventDefault();
  alert('Formulaire envoyé (le Studio ne doit jamais le déclencher en mode édition)');
});
"""

PAGE = """<!DOCTYPE html>
<html lang="fr">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><meta name="description" content="{description}">
<link rel="stylesheet" href="/assets/site.css"></head>
<body>
  <header>
    <a href="/" class="logo"><strong>Dupont</strong></a>
    <nav><a href="/">Accueil</a><a href="/about/">Notre histoire</a>
      <button type="button" class="btn" data-menu><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M4 6h16M4 12h16M4 18h16"/></svg> Menu</button></nav>
    <div class="menu-panel"><a href="/about/">Le fournil</a></div>
  </header>
  <section class="hero">
    <div>
      <h1>{heading} <span class="accent">vivant</span> du quartier</h1>
      <p><strong>Ouvert 7j/7</strong> de 6h à 20h, au 12 rue du Four.<br>Commandes la veille avant 18h.</p>
      <a class="btn" href="/contact.html"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M5 12h14M12 5l7 7-7 7"/></svg> Réserver une fournée</a>
    </div>
    <div class="media"><img src="/assets/hero.svg" alt="Pains au levain sur une planche"><span class="badge">Cuit ce matin</span></div>
  </section>
  <div class="stats"><div class="stat"><strong>250+</strong><span>pains par jour</span></div>
    <div class="stat"><strong>1987</strong><span>année d'ouverture</span></div></div>
  <table><tr><td>Baguette tradition</td><td>1,30 €</td></tr><tr><td>Pain de campagne</td><td>4,80 €</td></tr></table>
  <form><input type="email" placeholder="Votre e-mail pour la lettre du fournil"><button class="btn" type="submit">S'inscrire</button></form>
  <footer><p>© 2026 Boulangerie Dupont · Lausanne</p></footer>
  <script src="/assets/site.js"></script>
</body></html>
"""

HERO = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 600"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#e9c9a1"/><stop offset="1" stop-color="#9c5a2e"/></linearGradient></defs>
<rect width="800" height="600" fill="url(#g)"/><ellipse cx="400" cy="330" rx="260" ry="120" fill="#7a4220" opacity=".55"/></svg>"""


def write_template(target: pathlib.Path) -> None:
    shutil.rmtree(target, ignore_errors=True)
    (target / "about").mkdir(parents=True)
    (target / "assets").mkdir()
    (target / "index.html").write_text(PAGE.format(
        title="Boulangerie Dupont — pain au levain à Lausanne", heading="Le pain",
        description="Boulangerie artisanale : pains au levain naturel cuits chaque matin."), encoding="utf-8")
    (target / "about" / "index.html").write_text(PAGE.format(
        title="Notre histoire — Boulangerie Dupont", heading="Notre histoire",
        description="Trois générations de boulangers au service du quartier."), encoding="utf-8")
    (target / "assets" / "site.css").write_text(STYLE, encoding="utf-8")
    (target / "assets" / "site.js").write_text(SCRIPT, encoding="utf-8")
    (target / "assets" / "hero.svg").write_text(HERO, encoding="utf-8")


class Site(http.server.SimpleHTTPRequestHandler):
    """Vitrine servie comme par Nginx : try_files $uri $uri/ /index.html."""

    root = "."

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=type(self).root, **kwargs)

    def log_message(self, *args: Any) -> None:
        return

    def send_head(self) -> Any:
        path = pathlib.Path(self.translate_path(self.path))
        if not path.exists() or (path.is_dir() and not (path / "index.html").exists()):
            self.path = "/index.html"
        return super().send_head()

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class PermissiveSupabase(kit.FakeSupabase):
    """Les autres tables (audience…) répondent vide : seul le contenu est simulé."""

    def table(self, name: str) -> Any:
        if name == db.TABLE_CONTENT:
            return kit.FakeQuery(self.store)
        empty = SimpleNamespace(data=[], count=0)
        chain = SimpleNamespace()
        for method in ("select", "insert", "upsert", "update", "delete", "eq", "neq", "gte", "lte", "order",
                       "limit", "range", "in_", "is_", "like"):
            setattr(chain, method, lambda *args, **kwargs: chain)
        chain.execute = lambda: empty
        return chain


def main_harness() -> None:
    work = pathlib.Path(tempfile.mkdtemp(prefix="waas-studio-"))
    repo, web_root, pristine = work / "repo", work / "site", work / "pristine"
    write_template(repo)
    shutil.copytree(repo, web_root)

    store = kit.FakeStore()
    db.supabase = PermissiveSupabase(store)
    db.get_user_by_username = lambda username: {"id": "admin-1", "username": username, "role": "admin"}
    db.get_site_by_id = lambda site_id: {"site_id": site_id, "client_id": "client-1", "name": SITE_NAME}
    site_url = f"http://localhost:{SITE_PORT}"
    engine_box: dict[str, Any] = {}

    def run_engine(*args: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = engine_box["engine"].main(list(args))
        return code, buffer.getvalue()

    class Container:
        status = "running"

        def exec_run(self, command: Any, **_: Any) -> SimpleNamespace:
            code, output = run_engine("--rehydrate")
            print(f"[banc] publication à chaud (code {code})")
            return SimpleNamespace(exit_code=code, output=output.encode("utf-8"))

        def restart(self) -> None:
            shutil.rmtree(web_root, ignore_errors=True)
            shutil.copytree(repo, web_root)
            run_engine()

    main.docker_client = SimpleNamespace(containers=SimpleNamespace(get=lambda name: Container()))

    harness = APIRouter()

    @harness.get("/harness", include_in_schema=False)
    def harness_login() -> RedirectResponse:
        token = jwt.encode({"sub": "agence", "role": "admin", "exp": int(time.time()) + 86400},
                           main.SECRET_KEY, algorithm=main.ALGORITHM)
        response = RedirectResponse(f"/studio?site={SITE_ID}", status_code=302)
        response.set_cookie("access_token", token, httponly=True, samesite="lax")
        return response

    @harness.get("/api/v1/portal/overview", include_in_schema=False)
    def harness_overview() -> dict[str, Any]:
        return {"user": {"username": "agence", "role": "admin"},
                "sites": [{"site_id": SITE_ID, "name": SITE_NAME}],
                "site": {"site_id": SITE_ID, "name": SITE_NAME, "url": site_url, "running": True,
                         "live_visitors": 2}}

    main.app.include_router(harness)
    routes = main.app.router.routes
    added = routes[-2:]
    del routes[-2:]
    routes[0:0] = added

    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=API_PORT, log_level="warning",
                                           lifespan="off"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)

    engine_box["engine"] = kit.load_engine(f"http://127.0.0.1:{API_PORT}", web_root, pristine)
    os.environ["BROWSER_API_URL"] = f"http://localhost:{API_PORT}"
    code, output = run_engine()
    print(output.strip().splitlines()[-1] if output.strip() else f"moteur : code {code}")

    Site.root = str(web_root)
    site_server = http.server.ThreadingHTTPServer(("127.0.0.1", SITE_PORT), Site)
    threading.Thread(target=site_server.serve_forever, daemon=True).start()
    print(f"\nStudio  : http://localhost:{API_PORT}/harness")
    print(f"Vitrine : {site_url}  (fichiers : {web_root})\nCtrl+C pour arrêter.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.should_exit = True
        site_server.shutdown()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main_harness()
