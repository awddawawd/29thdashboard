"""Tests du cockpit porté (rendu des pages, logique métier, télémétrie, portail client).

Exécution : .venv\\Scripts\\python.exe tests\\test_cockpit.py
Aucun accès réseau : Supabase et Docker sont remplacés par des doublures.
"""

from __future__ import annotations

import os
import pathlib
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "End"))
os.chdir(ROOT / "End")

os.environ.setdefault("WAAS_SYNC_SECRET", "secret-de-test-cockpit")

import cockpit  # noqa: E402
import db  # noqa: E402
import main  # noqa: E402
import pages  # noqa: E402
import telemetry as T  # noqa: E402
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
# 1. Inclusions de fragments
# ---------------------------------------------------------------------------

def test_includes() -> None:
    section("1. Inclusions <!-- @include --> : paramètres, échappement, confinement")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        comps = root / "components"
        (comps / "admin").mkdir(parents=True)
        (comps / "title.html").write_text("<h1>{{title}}</h1>", encoding="utf-8")
        (comps / "outer.html").write_text("<header><!-- @include title.html --></header>", encoding="utf-8")
        (root / "secret.txt").write_text("SECRET", encoding="utf-8")

        out = pages.resolve_includes('<!-- @include title.html title="Sites & <b>" -->', comps)
        check("paramètre injecté et échappé", out == "<h1>Sites &amp; &lt;b&gt;</h1>", out)

        out = pages.resolve_includes('<!-- @include outer.html title="Imbriqué" -->', comps)
        check("inclusion imbriquée héritant des paramètres", out == "<header><h1>Imbriqué</h1></header>", out)

        out = pages.resolve_includes("<!-- @include ../secret.txt -->", comps)
        check("sortie du dossier components refusée", "SECRET" not in out and "introuvable" in out, out)

        out = pages.resolve_includes("<p>{{inconnu}}</p>", comps)
        check("paramètre inconnu remplacé par une chaîne vide", out == "<p></p>", out)

        out = pages.resolve_includes("<!-- @include absent.html -->", comps)
        check("fragment absent signalé en commentaire", out.startswith("<!-- include introuvable"), out)


def test_page_location() -> None:
    section("2. Localisation des pages (1 dossier = 1 index.html)")
    check("dossier -> index.html", main.locate_page(main.ADMIN_DIR, "dashboard") == main.ADMIN_DIR / "dashboard" / "index.html")
    check("sous-dossier -> index.html",
          main.locate_page(main.ADMIN_DIR, "sites/detail") == main.ADMIN_DIR / "sites" / "detail" / "index.html")
    check("remontée hors du dossier refusée", main.locate_page(main.PORTAL_DIR, "../admin/dashboard") is None)
    check("fichier caché refusé", main.locate_page(main.FRONT_DIR, ".env") is None)
    check("fragments jamais servis comme page", main.locate_page(main.FRONT_DIR, "components/head.html") is None)
    check("page inexistante", main.locate_page(main.ADMIN_DIR, "inexistante") is None)


def test_error_page_escaping() -> None:
    section("3. Page d'erreur : aucune XSS réfléchie")
    response = main.render_error_page(500, "Erreur 500", "<script>alert(1)</script>", "/login", "Retour")
    body = response.body.decode("utf-8")
    check("message échappé", "<script>alert(1)</script>" not in body and "&lt;script&gt;" in body)
    check("gabarit du cockpit utilisé", "ck-auth__card" in body)
    check("aucune directive d'inclusion résiduelle", "@include" not in body)


# ---------------------------------------------------------------------------
# 4. Logique métier (cockpit.py)
# ---------------------------------------------------------------------------

def test_business_logic() -> None:
    section("4. Logique métier : échéances, catalogue, KPIs, incidents")
    today = date(2026, 9, 30)
    check("anniversaire mensuel borné au 30 (mois court)", cockpit.next_monthly(date(2026, 1, 31), today) == date(2026, 9, 30))
    check("anniversaire mensuel (fin février)", cockpit.next_monthly(date(2026, 1, 31), date(2026, 2, 2)) == date(2026, 2, 28))
    check("date ISO tolérante", cockpit.parse_date("2026-09-26T16:32:13Z") == date(2026, 9, 26))
    check("date invalide -> None", cockpit.parse_date("hier") is None)

    records = [
        {"id": "a", "name": "Alpha", "plan_id": "standard", "plan_name": "Standard", "price_monthly": 49,
         "contract_status": "active", "contract_since": "2026-09-15", "domain": "a.example.ch",
         "domain_expires_at": "2026-10-10", "client_id": "c1"},
        {"id": "b", "name": "Bêta", "plan_id": "none", "price_monthly": 0, "contract_status": "active"},
        {"id": "z", "name": "Zêta", "price_monthly": 89, "contract_status": "cancelled"},
    ]
    inventory = [
        {"site_id": "a", "status": "running", "running": True, "port": "32001", "memory_mb": 40.0},
        {"site_id": "orphan", "status": "restarting", "running": False, "port": None},
    ]
    clients = [{"id": "c1", "username": "alice", "sites": [{"site_id": "a"}]}]
    catalog = cockpit.build_catalog(records, inventory, clients, {"a": 3},
                                    lambda rec, port: f"http://h:{port}" if port else None)
    by_id = {s["site_id"]: s for s in catalog}
    check("fiche + conteneur fusionnés", by_id["a"]["status"] == "running" and by_id["a"]["live_visitors"] == 3)
    check("fiche sans conteneur -> sans serveur", by_id["b"]["status"] == "not_provisioned")
    check("conteneur sans fiche conservé", "orphan" in by_id and not by_id["orphan"]["has_record"])
    check("propriétaire résolu", by_id["a"]["client_name"] == "alice")

    kpis = cockpit.business_kpis(catalog)
    check("MRR = contrats actifs uniquement (résilié exclu)", kpis["mrr"] == 49.0, str(kpis))
    check("ARPU sur contrats payants", kpis["arpu"] == 49.0, str(kpis))

    invoices = [{"number": "FAC-2026-0001", "site_id": "a", "status": "pending", "amount": 49,
                 "due_at": "2026-09-20", "issued_at": "2026-09-06"}]
    flagged = cockpit.flag_overdue(invoices, today)
    check("facture échue présentée « en retard »", flagged[0]["status"] == "overdue")
    renewals = cockpit.collect_renewals(catalog, flagged, today, "example.ch")
    kinds = [r["kind"] for r in renewals]
    check("facture impayée en tête (datée, la plus ancienne)", renewals[0]["kind"] == "invoice", str(kinds))
    check("domaine géré sans date -> automatique",
          any(r["kind"] == "domain" and r["state"] == "auto" for r in renewals if r["site_id"] == "a") is False
          and any(r["kind"] == "domain" and r["due"] == "2026-10-10" for r in renewals))
    check("abonnement résilié ignoré", not any(r["site_id"] == "z" and r["kind"] == "subscription" for r in renewals))
    rk = cockpit.renewal_kpis(renewals)
    check("échéances urgentes comptées", rk["urgent"] >= 2 and rk["overdue"] == 1, str(rk))

    health = {"cached_at": "x", "infrastructure": {"database": {"status": "UP", "latency_ms": 20},
                                                  "docker_engine": {"status": "DOWN"}},
              "fleet": {"alerts": []}, "external_services": {"failing": []}}
    incidents = cockpit.build_incidents({"cpu_percent": 95, "ram_percent": 50, "disk_percent": 86}, health, catalog,
                                        schema_missing=["invoices"])
    ids = [i["id"] for i in incidents]
    check("CPU critique détecté", "cpu_danger" in ids, str(ids))
    check("disque restreint détecté", "disk_warn" in ids, str(ids))
    check("Docker injoignable détecté", "docker_down" in ids, str(ids))
    check("crashloop détecté", "container_crashed" in ids, str(ids))
    check("migration manquante signalée", "schema_missing" in ids, str(ids))
    check("incidents critiques en premier", incidents[0]["level"] == "danger")
    summary = cockpit.alerts_summary(incidents)
    check("carte d'alertes au ton critique", summary["tone"] == "danger" and summary["count"] >= 3, str(summary))
    check("aucun incident -> nominal", cockpit.alerts_summary([])["tone"] == "good")

    ticket = {"id": "t", "site_id": "a", "subject": "S", "status": "open", "priority": "normal", "messages": [
        {"from_role": "client", "body": "x" * 200, "seen_at": None},
        {"from_role": "agency", "body": "Réponse", "seen_at": None},
    ]}
    shaped = cockpit.shape_ticket(ticket, {"a": "Alpha"})
    check("non-lus = messages client non vus (vue agence)", shaped["unread"] == 1, str(shaped["unread"]))
    check("extrait du dernier message", shaped["snippet"] == "Réponse")
    check("messages masqués sans demande explicite", "messages" not in shaped)


# ---------------------------------------------------------------------------
# 5. Télémétrie : trafic, disponibilité, historique, inventaire
# ---------------------------------------------------------------------------

def test_telemetry_aggregators() -> None:
    section("5. Agrégateurs de télémétrie")
    now = time.time()
    stamp = lambda seconds_ago: datetime.fromtimestamp(now - seconds_ago, timezone.utc).isoformat()  # noqa: E731

    traffic = T.TrafficAggregator()
    traffic.ingest([
        {"id": 1, "tenant_id": "a", "visitor_id": "v1", "created_at": stamp(30)},
        {"id": 2, "tenant_id": "a", "visitor_id": "v1", "created_at": stamp(20)},
        {"id": 3, "tenant_id": "a", "visitor_id": "v2", "created_at": stamp(4000)},
        {"id": 4, "tenant_id": "b", "visitor_id": "w1", "created_at": stamp(86400 * 30)},   # hors fenêtre
    ])
    traffic.prune(now)
    series = traffic.series(now=now)
    check("dernier identifiant mémorisé (lecture incrémentale)", traffic.last_id == 4)
    check("14 jours exactement", len(series) == 14)
    check("pages vues du site a", sum(p["pageviews"] for p in traffic.series("a", now)) == 3)
    check("fenêtre de 14 jours appliquée", sum(p["pageviews"] for p in traffic.series("b", now)) == 0)
    check("visiteurs en direct (< 5 min)", traffic.live("a", now) == 1 and traffic.live(now=now) == 1)

    uptime = T.UptimeRecorder()
    day1 = datetime(2026, 9, 29, 23, 59, tzinfo=timezone.utc).timestamp()
    day2 = datetime(2026, 9, 30, 0, 1, tzinfo=timezone.utc).timestamp()
    up = T.ProbeOutcome(key="site:a", label="A", kind="site", state=T.UPTIME_UP, latency_ms=10)
    uptime.record([up, T.ProbeOutcome(key="site:b", label="B", kind="site", state=T.UPTIME_SKIPPED)], now=day1)
    uptime.record([T.ProbeOutcome(key="site:a", label="A", kind="site", state=T.UPTIME_DOWN)], now=day2)
    pending = uptime.pending_rows()
    check("la veille reste à persister après minuit", any(r["day"] == "2026-09-29" for r in pending), str(pending))
    summary = uptime.summary("site:a", days=2, now=day2)
    check("disponibilité calculée sur les contrôles", summary["uptime"] == 50.0, str(summary["uptime"]))
    check("état courant = dernier contrôle", summary["current_state"] == T.UPTIME_DOWN)
    skipped = uptime.summary("site:b", days=2, now=day2)
    check("arrêt volontaire jamais compté en panne", skipped["applicable"] == 0)

    history = T.HostHistory()
    base = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc).timestamp()
    for i in range(3):
        history.add({"cpu_percent": 10 * (i + 1), "ram_percent": 50}, 100.0, now=base + i * 60)
    timeline = history.timeline("1h", now=base + 180)
    cpu = [v for v in timeline["series"][0]["values"] if v is not None]
    check("série minute en mémoire", cpu == [10.0, 20.0, 30.0], str(cpu))
    check("heure courante à persister", len(history.pending_rows()) == 1)

    container = SimpleNamespace(
        name="waas-alpha", short_id="abc123def456", status="exited", labels={"tenant.id": "alpha"},
        attrs={"State": {"Status": "exited", "ExitCode": 1}, "Config": {"Env": ["REPO_URL=https://github.com/o/r.git", "SYNC_TOKEN=secret"]},
               "HostConfig": {"Memory": 256 * 1024 * 1024}, "NetworkSettings": {"Ports": {}}},
    )
    info = T.describe_container(container)
    check("sortie en erreur -> error", info["status"] == "error")
    check("jeton de synchronisation jamais exposé", "secret" not in str(info))
    check("limite mémoire lue", info["memory_limit_mb"] == 256)
    container.attrs["State"]["ExitCode"] = 137
    check("arrêt demandé (137) -> stopped", T.describe_container(container)["status"] == "stopped")


# ---------------------------------------------------------------------------
# 6. Surface HTTP : zones protégées, redirections, propriété côté portail
# ---------------------------------------------------------------------------

USERS = {
    "alice": {"id": "c1", "username": "alice", "role": "client", "tenant_id": None},
    "bob": {"id": "c2", "username": "bob", "role": "client", "tenant_id": None},
    "admin": {"id": "a0", "username": "admin", "role": "admin", "tenant_id": None},
    "jean.dupont": {"id": "a1", "username": "jean.dupont", "role": "admin", "tenant_id": None},
}


def token(name: str) -> str:
    user = USERS[name]
    return main.create_access_token({"sub": user["username"], "role": user["role"]})


def test_http_surface() -> None:
    section("6. Surface HTTP & isolation des données du portail")
    saved = {name: getattr(db, name) for name in (
        "get_user_by_username", "list_site_records", "get_all_clients", "get_sites_by_client_id",
        "get_site_by_id", "get_invoice", "list_invoices", "list_tickets", "get_site_record",
    )}
    records = [{"id": "site-a", "site_id": "site-a", "name": "Site A", "client_id": "c1", "price_monthly": 49,
                "contract_status": "active"},
               {"id": "site-b", "site_id": "site-b", "name": "Site B", "client_id": "c2", "price_monthly": 49,
                "contract_status": "active"}]
    try:
        db.get_user_by_username = lambda username: dict(USERS[username]) if username in USERS else None
        db.list_site_records = lambda: [dict(r) for r in records]
        db.get_site_record = lambda site_id: next((dict(r) for r in records if r["id"] == site_id), None)
        db.get_all_clients = lambda: []
        db.get_sites_by_client_id = lambda client_id: [dict(r) for r in records if r["client_id"] == client_id]
        db.get_site_by_id = lambda site_id: next((dict(r) for r in records if r["id"] == site_id),
                                                 {"site_id": site_id, "id": site_id, "client_id": None})
        db.get_invoice = lambda invoice_id: {"id": "inv-b", "site_id": "site-b", "status": "pending", "amount": 49} \
            if invoice_id == "inv-b" else None
        db.list_invoices = lambda site_id=None, site_ids=None: []
        db.list_tickets = lambda site_id=None, site_ids=None, include_closed=True: []
        main._invalidate_cache()

        client = TestClient(main.app)
        check("/admin sans session -> 404 (zéro fuite)", client.get("/admin/dashboard").status_code == 404)
        answer = client.get("/portal", headers={"Accept": "text/html"}, follow_redirects=False)
        check("/portal sans session -> connexion", answer.status_code == 302
              and answer.headers["location"] == "/login?redirect=/portal", answer.headers.get("location"))
        check("fragments non servis", client.get("/components/head.html").status_code == 404)
        check("page de statut publique", client.get("/status").status_code == 200)

        alice = {"Authorization": f"Bearer {token('alice')}"}
        admin = {"Authorization": f"Bearer {token('admin')}"}
        check("client refusé sur le cockpit", client.get("/admin/dashboard", headers=alice).status_code == 404)
        check("agence servie sur le cockpit", client.get("/admin/dashboard", headers=admin).status_code == 200)
        page = client.get("/admin/dashboard", headers=admin).text
        check("page rendue sans directive résiduelle", "@include" not in page and "{{" not in page)

        # Identité rendue par le serveur : la coque est juste dès la première
        # peinture (aucun scintillement de nom ou d'initiales à la navigation).
        def shell_text(markup: str, field: str) -> str | None:
            match = re.search(rf'data-shell="{field}"[^>]*>([^<]*)<', markup)
            return match.group(1) if match else None

        dotted = {"Authorization": f"Bearer {token('jean.dupont')}"}
        shell_page = client.get("/admin/dashboard", headers=dotted).text
        check("coque : nom du compte rendu par le serveur",
              shell_text(shell_page, "username") == "jean.dupont", str(shell_text(shell_page, "username")))
        check("coque : initiales identiques à UI.initials",
              shell_text(shell_page, "avatar") == "JD", str(shell_text(shell_page, "avatar")))
        portal_page = client.get("/portal", headers=alice).text
        check("coque portail : nom du client rendu par le serveur",
              shell_text(portal_page, "username") == "alice", str(shell_text(portal_page, "username")))
        check("coque portail : initiales du client",
              shell_text(portal_page, "avatar") == "A", str(shell_text(portal_page, "avatar")))

        overview = client.get("/api/v1/portal/overview", headers=alice).json()
        visible = [s["site_id"] for s in overview["sites"]]
        check("le client ne voit que ses sites", visible == ["site-a"], str(visible))
        check("champs d'infrastructure masqués au client", "repo_url" not in (overview["site"] or {}))
        forced = client.get("/api/v1/portal/overview?site_id=site-b", headers=alice).json()
        check("?site_id d'un autre client ignoré", forced["site"]["site_id"] == "site-a")
        check("facture d'un autre client introuvable", client.get("/api/v1/invoices/inv-b", headers=alice).status_code == 404)
        check("tickets d'un autre site introuvables",
              client.get("/api/v1/sites/site-b/tickets", headers=alice).status_code == 404)
        admin_view = client.get("/api/v1/portal/overview", headers=admin).json()
        check("l'agence voit toutes les vitrines", len(admin_view["sites"]) == 2)

        payload = {"password": "x", "username": "alice"}
        response = client.post("/api/login", json=payload)
        check("connexion invalide refusée", response.status_code in (401, 429))
    finally:
        for name, value in saved.items():
            setattr(db, name, value)
        main._invalidate_cache()


def test_login_redirects() -> None:
    section("7. Redirections après connexion (anti open-redirect, rôles)")
    original = db.authenticate_user
    client = TestClient(main.app)
    try:
        cases = [
            ("client", "/admin/dashboard", "/portal"),
            ("client", "/portal/billing?site=a", "/portal/billing?site=a"),
            ("client", "//evil.example", "/portal"),
            ("admin", "/portal?site=a", "/portal?site=a"),
            ("admin", "/studio?site=a", "/studio?site=a"),
            ("admin", "https://evil.example", "/admin/dashboard"),
            ("admin", "", "/admin/dashboard"),
        ]
        for role, redirect, expected in cases:
            db.authenticate_user = lambda u, p, role=role: {"id": "x", "username": f"u-{role}", "role": role}
            data = client.post("/api/login", json={"username": f"u-{role}-{redirect}", "password": "p", "redirect": redirect}).json()
            check(f"{role} + {redirect or '(vide)'} -> {expected}", data.get("redirect_url") == expected, str(data.get("redirect_url")))
    finally:
        db.authenticate_user = original


# ---------------------------------------------------------------------------
# 8. Correctifs de la revue : propriété, CSRF/CORS, fuites, idempotence, bornes
# ---------------------------------------------------------------------------

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


def test_single_ownership() -> None:
    section("8a. Propriété d'un site : la fiche fait foi, tenant_id périmé ignoré")
    records = {"site-a": {"id": "site-a", "client_id": "c1"}, "site-b": {"id": "site-b", "client_id": "c2"},
               "site-free": {"id": "site-free", "client_id": None}}
    users = {"c1": {"id": "c1", "username": "alice", "tenant_id": "site-b"},      # rattachement périmé
             "c3": {"id": "c3", "username": "carol", "tenant_id": "legacy"}}      # site historique sans fiche

    def fake_read(relation, query, default, strict=False):  # requête du client c1 sur `sites`
        return [dict(r) for r in records.values() if r["client_id"] == fake_read.client] or default

    def sites_of(client_id: str) -> list[str]:
        fake_read.client = client_id
        return sorted(s["site_id"] for s in db.get_sites_by_client_id(client_id))

    with Patch(db, _read=fake_read, get_user_by_id=lambda uid: users.get(uid),
               get_site_record=lambda sid: {**records[sid], "site_id": sid} if sid in records else None):
        check("tenant_id périmé n'ouvre pas le site d'un autre", sites_of("c1") == ["site-a"], str(sites_of("c1")))
        check("site historique sans fiche conservé", sites_of("c3") == ["legacy"], str(sites_of("c3")))
        fake_read.client = "c1"
        stale = {"id": "c1", "role": "client", "tenant_id": "site-b"}
        check("routes /waas/my-site : site réellement possédé", main.require_client_tenant_id(stale) == "site-a")

    clients = [{"id": "c1", "username": "alice", "tenant_id": "site-free"},
               {"id": "c2", "username": "bob", "tenant_id": None}]
    catalog = {e["site_id"]: e for e in cockpit.build_catalog(
        [{"id": "site-free", "client_id": None}, {"id": "site-b", "client_id": "c2"}], [], clients, {},
        lambda rec, port: None)}
    check("fiche non attribuée : pas de propriétaire hérité", catalog["site-free"]["client_id"] is None
          and catalog["site-free"]["client_name"] == "")
    check("fiche attribuée : propriétaire de la fiche", catalog["site-b"]["client_name"] == "bob")


def test_csrf_and_cors() -> None:
    section("8b. CSRF (même port en local) et CORS (collecte d'audience seule)")

    def csrf(origin: str, host: str) -> bool:
        request = SimpleNamespace(method="POST", headers={"origin": origin, "host": host}, url=SimpleNamespace(path="/x"))
        try:
            main.verify_csrf_origin(request)
            return True
        except main.HTTPException:
            return False

    check("localhost -> 127.0.0.1 même port accepté", csrf("http://localhost:8000", "127.0.0.1:8000"))
    check("vitrine locale sur un autre port refusée", not csrf("http://localhost:32001", "localhost:8000"))
    check("origine externe refusée", not csrf("https://evil.example", "localhost:8000"))

    client = TestClient(main.app)
    preflight = {"Origin": "https://boulangerie.example", "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": "content-type"}
    answer = client.options("/waas/analytics/hit/site-a", headers=preflight)
    check("pré-vol de la collecte accepté depuis un domaine client",
          answer.status_code == 204 and answer.headers.get("access-control-allow-origin") == "https://boulangerie.example")
    answer = client.options("/admin/api/sites", headers={**preflight, "Origin": "http://localhost:32001"})
    check("API d'administration fermée aux vitrines locales", "access-control-allow-origin" not in answer.headers)
    answer = client.options("/admin/api/sites", headers={**preflight, "Origin": "http://localhost:3000"})
    check("serveur de dev (port 3000) toujours autorisé",
          answer.headers.get("access-control-allow-origin") == "http://localhost:3000")

    hits: list[str] = []
    with Patch(db, record_hit=lambda tenant_id, visitor_id, path: hits.append(tenant_id)):
        client.post("/waas/analytics/hit/Site_Invalide", json={"path": "/"})
        client.post("/waas/analytics/hit/site-a", json={"path": "/"}, headers={"X-Forwarded-For": "1.2.3.4"})
    check("identifiant hors format ignoré", hits == ["site-a"], str(hits))


def test_client_exposure() -> None:
    section("8c. Portail : champs internes et brouillons jamais exposés au client")
    record = {"id": "site-a", "site_id": "site-a", "client_id": "c1", "name": "Site A", "notes": "impayé 2x",
              "repo_url": "https://github.com/agence/prive.git", "domain_registrar": "Infomaniak"}
    invoices = [{"id": "d", "site_id": "site-a", "status": "draft", "amount": 10},
                {"id": "p", "site_id": "site-a", "status": "pending", "amount": 49, "due_at": "2999-01-01"}]
    with Patch(db, get_user_by_username=lambda u: dict(USERS[u]) if u in USERS else None,
               get_sites_by_client_id=lambda cid: [dict(record)] if cid == "c1" else [],
               get_site_by_id=lambda sid: dict(record) if sid == "site-a" else None,
               list_invoices=lambda site_id=None, site_ids=None: [dict(i) for i in invoices]):
        client = TestClient(main.app)
        alice = {"Authorization": f"Bearer {token('alice')}"}
        admin = {"Authorization": f"Bearer {token('admin')}"}
        me = client.get("/api/v1/account/me", headers=alice).json()
        leaked = [k for k in ("notes", "repo_url", "domain_registrar") if k in str(me)]
        check("/account/me sans notes, dépôt ni registrar", not leaked, str(leaked))
        answer = client.get("/api/v1/sites/site-a/invoices", headers=alice).json()
        check("brouillons masqués au client", [i["id"] for i in answer["invoices"]] == ["p"], str(answer["invoices"]))
        answer = client.get("/api/v1/sites/site-a/invoices", headers=admin).json()
        check("brouillons visibles de l'agence", len(answer["invoices"]) == 2)
    with Patch(db, get_setting=lambda key, default=None: default):
        check("signature agence (jamais l'identifiant admin)",
              main.author_label({"role": "admin", "username": "andi"}) == main.DEFAULT_AGENCY_NAME)


def test_invoice_generation() -> None:
    section("8d. Facturation mensuelle idempotente")
    created: list[dict] = []
    records = [{"id": s, "name": s, "client_id": None, "price_monthly": 49, "contract_status": "active",
                "plan_id": "standard"} for s in ("site-a", "site-b")]
    with Patch(db, list_site_records=lambda: [dict(r) for r in records], get_all_clients=lambda: [],
               subscription_invoiced_sites=lambda period, since_day: {"site-a"},
               create_invoice=lambda fields: created.append(fields) or fields):
        main._invalidate_cache()
        client = TestClient(main.app)
        admin = {"Authorization": f"Bearer {token('admin')}"}
        with Patch(db, get_user_by_username=lambda u: dict(USERS[u]) if u in USERS else None):
            answer = client.post("/admin/api/invoices/generate", headers=admin)
            check("seul le site non facturé ce mois-ci reçoit sa facture",
                  answer.status_code == 200 and [c["site_id"] for c in created] == ["site-b"], answer.text[:200])
            item = created[0]["items"][0] if created else {}
            check("ligne marquée abonnement + période", item.get("kind") == "subscription"
                  and item.get("period") == cockpit.today_utc().strftime("%Y-%m"), str(item))
            main._INVOICE_GENERATION_LOCK.acquire()
            try:
                busy = client.post("/admin/api/invoices/generate", headers=admin).status_code
            finally:
                main._INVOICE_GENERATION_LOCK.release()
            check("génération concurrente refusée (409)", busy == 409, str(busy))
    main._invalidate_cache()


def test_bounds_and_caches() -> None:
    section("8e. Bornes mémoire, oubli des sites purgés, cache, console, reconstruction")
    traffic = T.TrafficAggregator()
    traffic.MAX_VISITORS_PER_DAY = 2
    now = time.time()
    stamp = datetime.fromtimestamp(now - 10, timezone.utc).isoformat()
    traffic.ingest([{"id": i, "tenant_id": "a", "visitor_id": v, "created_at": stamp}
                    for i, v in enumerate(["v1", "v2", "v3", "v1"], start=1)])
    visitors = sum(p["visitors"] for p in traffic.series("a", now))
    check("visiteurs du jour bornés en mémoire (compte approché au-delà)", visitors == 3, str(visitors))
    traffic.forget("a")
    check("trafic d'un site purgé oublié", sum(p["pageviews"] for p in traffic.series("a", now)) == 0)

    uptime = T.UptimeRecorder()
    uptime.record([T.ProbeOutcome(key="site:x", label="X", kind="site", state=T.UPTIME_UP)])
    uptime.forget("site:x")
    check("disponibilité d'un site purgé oubliée", "site:x" not in uptime.keys() and not uptime.pending_rows())

    def stale_loader() -> str:
        main._invalidate_cache()          # une écriture survient pendant la lecture
        return "périmé"

    main._cached_read("clé-test", stale_loader)
    check("lecture antérieure à une écriture jamais remise en cache",
          main._cached_read("clé-test", lambda: "frais") == "frais")
    main._invalidate_cache()

    with Patch(main, api_public_url_setting=lambda: "https://x</script><script>alert(1)//"):
        import asyncio
        body = asyncio.run(main.render_page(main.FRONT_DIR / "status" / "index.html")).body.decode("utf-8")
    check("URL d'API injectée sans fermeture de balise possible", "</script><script>alert" not in body
          and "\\u003c/script>" in body)

    with Patch(main, docker_client=SimpleNamespace()), \
            Patch(db, get_user_by_username=lambda u: dict(USERS[u]) if u in USERS else None):
        taken = 0
        while main._EXEC_SLOTS.acquire(blocking=False):
            taken += 1
        try:
            answer = TestClient(main.app).post("/admin/api/sites/site-a/exec", json={"command": "ls"},
                                               headers={"Authorization": f"Bearer {token('admin')}"})
        finally:
            for _ in range(taken):
                main._EXEC_SLOTS.release()
        check("console saturée -> 429 (pool dédié borné)", answer.status_code == 429, str(answer.status_code))

    events: list[str] = []

    class FakeContainer:
        def __init__(self, name: str) -> None:
            self.name, self.status, self.attrs = name, "running", {"Config": {"Env": []}}

        def rename(self, name: str) -> None:
            events.append(f"rename:{name.split('-rebuild-')[0]}{'-bak' if '-rebuild-' in name else ''}")
            self.name = name

        def stop(self, timeout: int = 10) -> None:
            events.append("stop")

        def start(self) -> None:
            events.append("start")

        def remove(self, **kwargs: object) -> None:
            events.append("remove")

    old = FakeContainer("waas-site-a")

    def get(name: str) -> FakeContainer:
        if old.name == name:
            return old
        raise main.NotFound("absent")

    def failing_provision(site_id: str, repo_url: str) -> None:
        raise main.APIError("image introuvable")

    with Patch(main, docker_client=SimpleNamespace(containers=SimpleNamespace(get=get)),
               provision_container=failing_provision, validate_repo_url=lambda url: url,
               check_repo_access=lambda url: None,
               validate_container_api_url=lambda url: url, api_public_url_setting=lambda: "http://api"), \
            Patch(db, get_site_record=lambda sid: {"repo_url": "https://github.com/o/r.git"}):
        try:
            main.rebuild_site("site-a")
            failed = False
        except main.APIError:
            failed = True
    check("reconstruction ratée : erreur remontée", failed)
    check("ancien conteneur restauré et relancé, jamais supprimé",
          events == ["rename:waas-site-a-bak", "stop", "rename:waas-site-a", "start"] and old.name == "waas-site-a",
          str(events))


# ---------------------------------------------------------------------------

def main_test() -> int:
    print("Tests du cockpit")
    test_includes()
    test_page_location()
    test_error_page_escaping()
    test_business_logic()
    test_telemetry_aggregators()
    test_http_surface()
    test_login_redirects()
    test_single_ownership()
    test_csrf_and_cors()
    test_client_exposure()
    test_invoice_generation()
    test_bounds_and_caches()

    print(f"\n{'=' * 60}")
    print(f"{CHECKS - len(FAILURES)}/{CHECKS} vérifications réussies")
    if FAILURES:
        print("ÉCHECS :")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("Cockpit vérifié.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_test())
