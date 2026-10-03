"""Tests de durcissement de l'API (lot « accès, réseau, en-têtes »).

Exécution : .venv\\Scripts\\python.exe tests\\test_api_hardening.py
"""

from __future__ import annotations

import os
import pathlib
import sys
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "End"))
os.chdir(ROOT / "End")

os.environ.setdefault("WAAS_SYNC_SECRET", "secret-de-test-api")

import db  # noqa: E402
import sync_auth  # noqa: E402
import main  # noqa: E402
from fastapi import HTTPException  # noqa: E402
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


# ---------------------------------------------------------------------------
# 1. Contrôle d'accès : refus par défaut
# ---------------------------------------------------------------------------

def test_site_ownership_fail_closed() -> None:
    section("1. verify_site_ownership : refus par défaut")

    request = SimpleNamespace(url=SimpleNamespace(path="/api/v1/sites/orphelin/content"))
    orphan = {"site_id": "orphelin", "id": "orphelin", "client_id": None, "name": "orphelin"}
    assigned = {"site_id": "site-1", "id": "site-1", "client_id": "client-1", "name": "site-1"}

    original = main.db.get_site_by_id
    try:
        main.db.get_site_by_id = lambda site_id: orphan                     # type: ignore[assignment]

        # Client sans tenant_id : auparavant autorisé, doit être refusé.
        intruder = {"id": "client-attaquant", "username": "x", "role": "client", "tenant_id": None}
        try:
            main.verify_site_ownership("orphelin", current_user=intruder, request=request)
            check("site sans propriétaire refusé au client", False, "accès accordé")
        except HTTPException as exc:
            check("site sans propriétaire refusé au client", exc.status_code == 404, str(exc.status_code))

        # Client avec tenant_id différent : refusé aussi.
        other = {"id": "client-2", "username": "y", "role": "client", "tenant_id": "autre"}
        try:
            main.verify_site_ownership("orphelin", current_user=other, request=request)
            check("client non propriétaire refusé", False, "accès accordé")
        except HTTPException as exc:
            check("client non propriétaire refusé", exc.status_code == 404)

        # Admin : accès conservé (gestion des sites orphelins).
        admin = {"id": "admin-1", "username": "admin", "role": "admin"}
        granted = main.verify_site_ownership("orphelin", current_user=admin, request=request)
        check("admin conserve l'accès aux sites orphelins", granted is orphan)

        # Propriétaire légitime : autorisé.
        main.db.get_site_by_id = lambda site_id: assigned                     # type: ignore[assignment]
        owner = {"id": "client-1", "username": "z", "role": "client", "tenant_id": "site-1"}
        granted = main.verify_site_ownership("site-1", current_user=owner, request=request)
        check("propriétaire légitime autorisé", granted is assigned)
    finally:
        main.db.get_site_by_id = original                                     # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 2. Validation de la source de contenu et de l'URL d'API
# ---------------------------------------------------------------------------

def test_deploy_validations() -> None:
    section("2. Validations de déploiement (dépôt Git, URL d'API)")

    for url in ("https://github.com/org/template.git", "https://gitlab.com/org/t.git"):
        try:
            main.validate_repo_url(url)
            check(f"dépôt accepté : {url}", True)
        except HTTPException as exc:
            check(f"dépôt accepté : {url}", False, exc.detail)

    refused = {
        "http://github.com/org/template.git": "schéma http",
        "https://evil.example/template.git": "hôte non autorisé",
        "https://user:token@github.com/org/t.git": "identifiants dans l'URL",
        "https://169.254.169.254/latest/meta-data/": "hôte non autorisé (SSRF)",
        "https://127.0.0.1:8000/internal/sync/content": "hôte local",
    }
    for url, reason in refused.items():
        try:
            main.validate_repo_url(url)
            check(f"dépôt refusé ({reason})", False, url)
        except HTTPException as exc:
            check(f"dépôt refusé ({reason})", exc.status_code == 422, f"{exc.status_code} {exc.detail}")

    original = main.db.get_setting
    main.db.get_setting = lambda key, default=None: (
        "github.com,git.example.internal" if key == "allowed_git_hosts" else (default if default is not None else "")
    )  # type: ignore[assignment]
    try:
        main.validate_repo_url("https://git.example.internal/org/t.git")
        check("liste blanche personnalisable par réglage", True)
    except HTTPException as exc:
        check("liste blanche personnalisable par réglage", False, exc.detail)
    finally:
        main.db.get_setting = original  # type: ignore[assignment]

    accepted = {"http://172.17.0.1:8000": True, "https://api.waas.example": True,
                "http://localhost:8000": False, "": False, "http://127.0.0.1:8000": False}
    for url, should_pass in accepted.items():
        try:
            main.validate_container_api_url(url)
            passed = True
        except HTTPException:
            passed = False
        check(f"URL d'API {'acceptée' if should_pass else 'refusée'} : {url!r}", passed is should_pass)


def test_browser_api_url() -> None:
    section("2b. URL d'API vue du navigateur (audience & aperçu du Studio)")

    # Le navigateur vit sur l'hôte : localhost y est la valeur normale, alors
    # qu'elle est justement refusée pour l'URL « conteneur ».
    for url in ("http://localhost:8000", "http://127.0.0.1:8000", "https://api.exemple.com",
                "api.exemple.com"):
        try:
            main.validate_browser_api_url(url)
            check(f"URL navigateur acceptée : {url!r}", True)
        except HTTPException as exc:
            check(f"URL navigateur acceptée : {url!r}", False, exc.detail)

    for url, reason in {
        "": "vide",
        "javascript:alert(1)": "schéma non http(s)",
        "http://api.exemple.com/\nSet-Cookie: x=1": "caractères d'en-tête",
        "http://api exem ple.com": "hôte invalide",
    }.items():
        try:
            main.validate_browser_api_url(url)
            check(f"URL navigateur refusée ({reason})", False, url)
        except HTTPException as exc:
            check(f"URL navigateur refusée ({reason})", exc.status_code == 422, str(exc.status_code))

    # frame-ancestors : origine du Studio, plus l'alias de bouclage (deux origines
    # distinctes pour un navigateur) ; rien de superflu hors bouclage.
    check("frame-ancestors localhost couvre 127.0.0.1",
          main.portal_frame_ancestors("http://localhost:8000") == "http://localhost:8000 http://127.0.0.1:8000",
          main.portal_frame_ancestors("http://localhost:8000"))
    check("frame-ancestors 127.0.0.1 couvre localhost",
          main.portal_frame_ancestors("http://127.0.0.1:8000") == "http://127.0.0.1:8000 http://localhost:8000",
          main.portal_frame_ancestors("http://127.0.0.1:8000"))
    check("frame-ancestors public : origine seule",
          main.portal_frame_ancestors("https://api.exemple.com") == "https://api.exemple.com",
          main.portal_frame_ancestors("https://api.exemple.com"))
    check("frame-ancestors repli sûr",
          main.portal_frame_ancestors("") == "'self'", main.portal_frame_ancestors(""))

    # Repli : sans réglage, l'URL navigateur suit l'URL des conteneurs.
    original = main.db.get_setting
    main.db.get_setting = lambda key, default=None: (
        "https://api.exemple.com" if key == "api_public_url" else (default if default is not None else "")
    )  # type: ignore[assignment]
    try:
        check("repli sur api_public_url", main.browser_api_url_setting() == "https://api.exemple.com",
              main.browser_api_url_setting())
        main.db.get_setting = lambda key, default=None: (
            {"api_public_url": "https://api.exemple.com", "browser_api_url": "http://localhost:8000"}.get(key)
            if key in ("api_public_url", "browser_api_url") else (default if default is not None else "")
        )  # type: ignore[assignment]
        check("le réglage dédié prime", main.browser_api_url_setting() == "http://localhost:8000",
              main.browser_api_url_setting())
    finally:
        main.db.get_setting = original  # type: ignore[assignment]


def test_port_helper() -> None:
    section("3. Découverte du port web (8080 ou 80)")

    def container(ports: dict):
        return SimpleNamespace(attrs={"NetworkSettings": {"Ports": ports}})

    check("port 8080 (image durcie)",
          main.get_container_web_port(container({"8080/tcp": [{"HostPort": "34567"}]})) == "34567")
    check("port 80 (conteneur historique)",
          main.get_container_web_port(container({"80/tcp": [{"HostPort": "41234"}]})) == "41234")
    check("8080 prioritaire si les deux existent",
          main.get_container_web_port(
              container({"8080/tcp": [{"HostPort": "1"}], "80/tcp": [{"HostPort": "2"}]})) == "1")
    check("aucun port publié", main.get_container_web_port(container({})) is None)


# ---------------------------------------------------------------------------
# 4. Limitation des tentatives de connexion
# ---------------------------------------------------------------------------

def test_login_throttle() -> None:
    section("4. Limiteur de tentatives de connexion")

    throttle = main.LoginThrottle(max_failures=3, window_seconds=60, lockout_seconds=60)
    key = "10.0.0.9|intrus"
    check("aucun blocage initial", throttle.retry_after(key) == 0.0)
    for _ in range(2):
        throttle.register_failure(key)
    check("blocage après 2 échecs (< seuil)", throttle.retry_after(key) == 0.0)
    throttle.register_failure(key)
    check("blocage après 3 échecs", throttle.retry_after(key) > 0)

    throttle.register_success(key)
    check("succès réinitialise le compteur", throttle.retry_after(key) == 0.0)

    other = "10.0.0.10|autre"
    throttle.register_failure(other)
    check("clés indépendantes (IP+identifiant)", throttle.retry_after(other) == 0.0)


# ---------------------------------------------------------------------------
# 5. Surface HTTP : auth de l'éditeur et en-têtes de sécurité
# ---------------------------------------------------------------------------

def test_http_surface() -> None:
    section("5. Surface HTTP (éditeur protégé, en-têtes, endpoints internes)")

    client = TestClient(main.app)

    for path in ("/editor", "/"):
        answer = client.get(path, follow_redirects=False)
        check(f"{path} exige une authentification", answer.status_code == 401, str(answer.status_code))

    answer = client.get("/openapi.json")
    check("nosniff sur les réponses", answer.headers.get("x-content-type-options") == "nosniff")
    check("anti-encadrement (X-Frame-Options)",
          answer.headers.get("x-frame-options") == "DENY")
    check("Referrer-Policy",
          answer.headers.get("referrer-policy") == "strict-origin-when-cross-origin")

    html = client.get("/login", follow_redirects=False)
    check("/login servi sans authentification", html.status_code == 200, str(html.status_code))
    check("CSP présente sur le HTML", "frame-ancestors 'none'" in (html.headers.get("content-security-policy") or ""),
          str(html.headers.get("content-security-policy"))[:80])

    # Coque : le cache de session évite de repeindre les défauts à chaque
    # navigation ; il ne doit donc jamais survivre à une session.
    app_js = (ROOT / "Front" / "js" / "app.js").read_text(encoding="utf-8")
    login_html = (ROOT / "Front" / "login" / "index.html").read_text(encoding="utf-8")
    check("coque : cache de session préfixé et purgé à la déconnexion",
          "waas:shell:" in app_js and 'a[href^="/logout"]' in app_js)
    check("coque : cache purgé sur la page de connexion", "waas:shell:" in login_html)
    check("coque : lecture du cache conditionnée à l'identité rendue par le serveur",
          "readShellCache(scope, nameNode ? nameNode.textContent.trim() : '')" in app_js)

    token = sync_auth.issue_tenant_token("boutique-a")
    check("endpoint interne toujours protégé par jeton",
          client.get("/internal/sync/content").status_code == 401)
    check("endpoint interne accessible avec jeton valide",
          client.get("/internal/sync/content",
                     headers={"X-WAAS-Sync-Token": token}).status_code in (200, 500, 502))

    spec_response = client.get("/openapi.json")
    if spec_response.status_code == 404:
        print("      (documentation desactivee — WAAS_ENABLE_DOCS=0 : controles OpenAPI ignores)")
    else:
        spec = spec_response.json()
        check("endpoints internes absents de la documentation publique",
              "/internal/sync/content" not in spec.get("paths", {}))


# ---------------------------------------------------------------------------

def main_test() -> int:
    print("Tests de durcissement de l'API")
    test_site_ownership_fail_closed()
    test_deploy_validations()
    test_browser_api_url()
    test_port_helper()
    test_login_throttle()
    test_http_surface()

    print(f"\n{'=' * 60}")
    print(f"{CHECKS - len(FAILURES)}/{CHECKS} vérifications réussies")
    if FAILURES:
        print("ÉCHECS :")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("Durcissement API vérifié.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_test())
