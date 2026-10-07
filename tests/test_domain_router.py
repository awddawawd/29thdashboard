"""Tests unitaires et d'intégration du routeur multi-domaines et de la bascule de mode.

Exécution : .venv/bin/python tests/test_domain_router.py
"""

from __future__ import annotations

import os
import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "End"))
os.chdir(ROOT / "End")

os.environ.setdefault("WAAS_SYNC_SECRET", "secret-test-domain-router")

import db  # noqa: E402
import domain_router as DR  # noqa: E402
import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

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


def test_domain_normalization() -> None:
    section("1. Normalisation et nettoyage de domaines")

    check("clean_domain vide", DR.clean_domain("") == "")
    check("clean_domain None", DR.clean_domain(None) == "")
    check("clean_domain avec https et slash", DR.clean_domain("https://29th.site/") == "29th.site")
    check("clean_domain avec port", DR.clean_domain("29th.site:8000") == "29th.site")
    check("clean_domain avec sous-domaine", DR.clean_domain("app.29th.site") == "app.29th.site")

    check("normalize_base_domain url brute", DR.normalize_base_domain("https://29th.site") == "29th.site")
    check("normalize_base_domain avec app.", DR.normalize_base_domain("app.29th.site") == "29th.site")
    check("normalize_base_domain avec api.", DR.normalize_base_domain("https://api.29th.site/") == "29th.site")
    check("normalize_base_domain avec www.", DR.normalize_base_domain("www.29th.site") == "29th.site")
    check("normalize_base_domain nu", DR.normalize_base_domain("29th.site") == "29th.site")

    invalid_threw = False
    try:
        DR.normalize_base_domain("invalid domain with spaces")
    except ValueError:
        invalid_threw = True
    check("normalize_base_domain invalide lève ValueError", invalid_threw)


def test_site_public_urls() -> None:
    section("2. Calcul des URLs publiques (Local vs Production)")

    # En mode local
    fake_settings = {"client_base_domain": "localhost", "landing_site_slug": ""}
    original_get_setting = db.get_setting
    db.get_setting = lambda k, d=None: fake_settings.get(k, d if d is not None else "")  # type: ignore[assignment]

    try:
        local_url = main.site_public_url({"site_id": "dwadaw"}, "32795")
        check("Mode local : URL sur port direct", local_url == "http://localhost:32795", local_url or "")

        # En mode production avec apex et sous-domaines
        fake_settings["client_base_domain"] = "29th.site"
        fake_settings["api_public_url"] = "https://api.29th.site"
        fake_settings["landing_site_slug"] = "dwadaw"

        # Le site officiel (dwadaw) est servi sur l'apex
        landing_url = main.site_public_url({"site_id": "dwadaw"}, "32795")
        check("Production : site vitrine sur l'apex", landing_url == "https://29th.site", landing_url or "")

        # Un autre locataire (client-a) est sur son sous-domaine {slug}.29th.site
        tenant_url = main.site_public_url({"site_id": "client-a"}, "32796")
        check("Production : locataire sur sous-domaine", tenant_url == "https://client-a.29th.site", tenant_url or "")

        # Un client avec son propre domaine personnalisé (ex. ma-boutique.ch)
        custom_url = main.site_public_url({"site_id": "custom-shop", "domain": "ma-boutique.ch"}, "32797")
        check("Production : domaine personnalisé propre", custom_url == "https://ma-boutique.ch", custom_url or "")
    finally:
        db.get_setting = original_get_setting  # type: ignore[assignment]


def test_csrf_subdomains() -> None:
    section("3. Protection anti-CSRF multi-sous-domaines")

    fake_settings = {"client_base_domain": "29th.site", "api_public_url": "https://api.29th.site"}
    original_get_setting = db.get_setting
    db.get_setting = lambda k, d=None: fake_settings.get(k, d if d is not None else "")  # type: ignore[assignment]

    try:
        # Requête légitime depuis app.29th.site vers api.29th.site
        req_legit = SimpleNamespace(
            method="POST",
            headers={"origin": "https://app.29th.site", "host": "api.29th.site"},
            url=SimpleNamespace(path="/admin/api/fleet/settings"),
        )
        passed = True
        try:
            main.verify_csrf_origin(req_legit)  # type: ignore[arg-type]
        except Exception as exc:
            passed = False
        check("Origine app.29th.site vers api.29th.site acceptée", passed)

        # Requête hostile depuis un sous-domaine locataire (dwadaw.29th.site) vers api.29th.site
        req_tenant = SimpleNamespace(
            method="POST",
            headers={"origin": "https://dwadaw.29th.site", "host": "api.29th.site"},
            url=SimpleNamespace(path="/admin/api/fleet/settings"),
        )
        blocked = False
        try:
            main.verify_csrf_origin(req_tenant)  # type: ignore[arg-type]
        except Exception:
            blocked = True
        check("Origine locataire dwadaw.29th.site vers api.29th.site bloquée", blocked)

        # Requête pirate externe (evil.com)
        req_evil = SimpleNamespace(
            method="POST",
            headers={"origin": "https://evil.com", "host": "api.29th.site"},
            url=SimpleNamespace(path="/admin/api/fleet/settings"),
        )
        blocked_evil = False
        try:
            main.verify_csrf_origin(req_evil)  # type: ignore[arg-type]
        except Exception:
            blocked_evil = True
        check("Origine pirate evil.com bloquée", blocked_evil)
    finally:
        db.get_setting = original_get_setting  # type: ignore[assignment]


def test_domain_router_middleware_logic() -> None:
    section("4. Routage HTTP des sous-domaines (Middleware)")

    fake_settings = {
        "client_base_domain": "29th.site",
        "api_public_url": "https://api.29th.site",
        "landing_site_slug": "dwadaw",
    }
    original_get_setting = db.get_setting
    db.get_setting = lambda k, d=None: fake_settings.get(k, d if d is not None else "")  # type: ignore[assignment]

    client = TestClient(main.app)

    try:
        # 1. Sous-domaine api.29th.site sur / -> JSON de statut
        resp_api_root = client.get("/", headers={"host": "api.29th.site"})
        check("api.29th.site / renvoie le statut JSON",
              resp_api_root.status_code == 200 and resp_api_root.json().get("service") == "WaaS API Engine",
              str(resp_api_root.status_code))

        # 2. Sous-domaine www.29th.site -> Redirection 301 vers 29th.site
        resp_www = client.get("/about", headers={"host": "www.29th.site"}, follow_redirects=False)
        check("www.29th.site redirige vers https://29th.site/about",
              resp_www.status_code == 301 and resp_www.headers.get("location") == "https://29th.site/about",
              f"{resp_www.status_code} {resp_www.headers.get('location')}")

        # 3. Chemins d'audience et sync bypassent le routeur d'hôte même sur un hôte inconnu
        resp_sync = client.post("/internal/sync/inventory", headers={"host": "dwadaw.29th.site"})
        # 401 attendu car aucun jeton HMAC n'est fourni, mais preuve que FastAPI a traité la requête (non 404 proxy)
        check("Bypass automatique pour /internal/sync/*", resp_sync.status_code == 401, str(resp_sync.status_code))

        # 4. Mode local désactivé : les requêtes localhost passent direct
        fake_settings["client_base_domain"] = "localhost"
        resp_local = client.get("/status", headers={"host": "localhost:8000"})
        check("Mode local : localhost passe directement à FastAPI", resp_local.status_code == 200, str(resp_local.status_code))
    finally:
        db.get_setting = original_get_setting  # type: ignore[assignment]


def test_switch_mode_api() -> None:
    section("5. Endpoint switch-mode (Bascule en un clic)")

    token = main.create_access_token(data={"sub": "admin", "role": "admin", "id": "adm-1"})
    client = TestClient(main.app)
    headers = {"Authorization": f"Bearer {token}"}

    # Bascule vers production
    with patch("main._update_env_file"):
        resp = client.post("/admin/api/fleet/switch-mode", json={
            "mode": "production",
            "base_domain": "https://29th.site/",
            "landing_site_slug": "dwadaw",
            "rebuild_containers": False,
        }, headers=headers)

        check("switch-mode production status 200", resp.status_code == 200, str(resp.status_code))
        data = resp.json()
        check("switch-mode mode = production", data.get("mode") == "production")
        check("base_domain normalisé en '29th.site'", data.get("environment", {}).get("base_domain") == "29th.site")
        urls = data.get("environment", {}).get("urls", {})
        check("URL showcase = https://29th.site", urls.get("showcase") == "https://29th.site")
        check("URL dashboard = https://app.29th.site", urls.get("dashboard") == "https://app.29th.site")
        check("URL api = https://api.29th.site", urls.get("api") == "https://api.29th.site")

    # Bascule retour vers local
    with patch("main._update_env_file"):
        resp_local = client.post("/admin/api/fleet/switch-mode", json={
            "mode": "local",
        }, headers=headers)

        check("switch-mode local status 200", resp_local.status_code == 200, str(resp_local.status_code))
        data_local = resp_local.json()
        check("switch-mode local mode = local", data_local.get("mode") == "local")
        check("local base_domain = localhost", data_local.get("environment", {}).get("base_domain") == "localhost")


def main_suite() -> int:
    test_domain_normalization()
    test_site_public_urls()
    test_csrf_subdomains()
    test_domain_router_middleware_logic()
    test_switch_mode_api()

    print("\n" + "=" * 60)
    print(f"{CHECKS - len(FAILURES)}/{CHECKS} vérifications réussies")
    if FAILURES:
        print("ÉCHECS :")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("Routeur de domaine et bascule de mode vérifiés avec succès.")
    return 0


if __name__ == "__main__":
    sys.exit(main_suite())
