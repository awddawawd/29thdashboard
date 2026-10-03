"""Vérifications ponctuelles (lecture seule) : absence d'authentification sur /editor
et comportement de verify_site_ownership quand l'appelant n'a pas de tenant_id."""
from __future__ import annotations

import os
import pathlib
import sys
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "End"))
os.chdir(ROOT / "End")

import main  # noqa: E402

print("=== 1. Dépendances déclarées sur /editor et /admin ===")
for route in main.app.routes:
    path = getattr(route, "path", None)
    if path in ("/editor", "/admin/api/fleet/health", "/admin/{subpath:path}"):
        deps = getattr(getattr(route, "dependant", None), "dependencies", []) or []
        names = [getattr(d.call, "__name__", repr(d.call)) for d in deps]
        print(f"  {path:32} methodes={sorted(getattr(route, 'methods', []) or [])} dependances={names}")

print("\n=== 2. verify_site_ownership avec un compte client SANS tenant_id ===")
fake_request = SimpleNamespace(url=SimpleNamespace(path="/api/v1/sites/site-victime/content"))
intruder = {"id": "client-attaquant", "username": "attaquant", "role": "client", "tenant_id": None}

site = main.db.get_site_by_id("site-victime-inexistant-9f3c")   # lecture seule réelle
print(f"  db.get_site_by_id(...) -> {site}")

try:
    granted = main.verify_site_ownership(
        site_id="site-victime-inexistant-9f3c", current_user=intruder, request=fake_request
    )
    print(f"  RESULTAT : accès AUTORISE -> {granted}")
except Exception as exc:  # HTTPException attendue si le contrôle fonctionne
    print(f"  RESULTAT : accès REFUSE -> {type(exc).__name__} {getattr(exc, 'detail', exc)}")

print("\n=== 3. Même vérification avec un compte client AVEC tenant_id ===")
legit = {"id": "client-1", "username": "client1", "role": "client", "tenant_id": "mon-site"}
try:
    granted = main.verify_site_ownership(
        site_id="site-victime-inexistant-9f3c", current_user=legit, request=fake_request
    )
    print(f"  RESULTAT : accès AUTORISE -> {granted}")
except Exception as exc:
    print(f"  RESULTAT : accès REFUSE -> {type(exc).__name__} {getattr(exc, 'detail', exc)}")
