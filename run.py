"""Serveur Flask de développement : sert une page de Front/ (connexion par défaut).

Usage : python run.py [Front/<page>]   (ex. python run.py Front/status)

Les inclusions <!-- @include … --> sont résolues par le même module que l'API
(End/pages.py) : aucun rendu dupliqué. Les pages authentifiées (admin, portail,
studio) se testent via l'API FastAPI (End/main.py), seule à appliquer le RBAC.
"""
import json
import sys
from pathlib import Path

from flask import Flask, Response, send_from_directory

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "End"))
import db  # noqa: E402
import pages  # noqa: E402

FRONT_DIR = (ROOT / "Front").resolve()
COMPONENTS_DIR = FRONT_DIR / "components"

# 1. URL de l'API (réglage en base) injectée dans la page
api_url = db.get_setting("api_public_url", "http://localhost:8000")

# 2. Dossier de la page servie (Front/login par défaut)
target_path = Path(sys.argv[1] if len(sys.argv) > 1 else "Front/login")
target_dir = (target_path.parent if target_path.is_file() else target_path).resolve()
if not target_dir.is_relative_to(FRONT_DIR):
    raise SystemExit(f"La page doit se trouver dans {FRONT_DIR}")

app = Flask(__name__)


@app.route("/")
@app.route("/index.html")
@app.route("/login")
def index() -> Response:
    html = pages.resolve_includes((target_dir / "index.html").read_text(encoding="utf-8"), COMPONENTS_DIR, {"v": "dev"})
    # Sérialisation JSON (valeur issue de la base) : évite toute évasion du littéral JS.
    # « < » échappé : la valeur ne doit jamais pouvoir fermer la balise (</script>).
    script_tag = f"<script>window.API_URL = {json.dumps(api_url).replace('<', '\\u003c')};</script>"
    return Response(html.replace("</head>", f"  {script_tag}\n</head>", 1), mimetype="text/html")


@app.route("/<path:filename>")
def static_files(filename: str) -> Response:
    if filename.split("/", 1)[0] == "components":
        return Response("Introuvable", status=404)
    return send_from_directory(FRONT_DIR, filename)


if __name__ == "__main__":
    print(f"Serveur Flask de développement : http://localhost:3000 (API_URL injectée : {api_url})")
    # debug=True activerait la console Werkzeug (exécution de code si exposée) :
    # ce serveur de développement ne doit rester joignable que sur la loopback.
    app.run(host="127.0.0.1", port=3000, debug=False)
