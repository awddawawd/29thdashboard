"""Logique métier du cockpit (sans I/O, sans FastAPI).

Port de l'ancien dashboard (OldDashBoard/services) sur le modèle de données de
l'API : fiche commerciale ``sites`` + conteneurs Docker + télémétrie en mémoire.
Toutes les fonctions sont pures : elles reçoivent des dictionnaires et en
renvoient, ce qui les rend testables sans Supabase ni Docker.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# Catalogue commercial
# ---------------------------------------------------------------------------

#: Formules proposées (identifiant, libellé, prix mensuel TTC en euros).
PLANS: tuple[dict[str, Any], ...] = (
    {"id": "none", "name": "Sans formule", "price": 0.0},
    {"id": "starter", "name": "Starter", "price": 39.0},
    {"id": "standard", "name": "Standard", "price": 49.0},
    {"id": "growth", "name": "Growth Pro", "price": 89.0},
)
PLANS_BY_ID = {plan["id"]: plan for plan in PLANS}

SOON_DAYS = 30
UNPAID_STATUSES = ("pending", "overdue")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def as_number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return default if parsed != parsed else parsed


def parse_date(value: Any) -> Optional[date]:
    """« 2026-09-26 », « 2026-09-26T16:32:13Z »… -> date ; ``None`` si absente ou invalide."""
    match = _ISO_DATE.match(str(value or "").strip())
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def url_host(url: str) -> str:
    value = (url or "").strip()
    if not value:
        return ""
    return urlsplit(value if "://" in value else f"//{value}").hostname or ""


# ---------------------------------------------------------------------------
# Catalogue des sites : fiche commerciale × conteneur × télémétrie
# ---------------------------------------------------------------------------

def client_names(clients: Iterable[dict[str, Any]]) -> dict[str, str]:
    """Identifiant client -> nom affiché."""
    return {str(c.get("id")): str(c.get("username") or "Client") for c in clients if c.get("id")}


def site_owner(
    site_id: str,
    record: Optional[dict[str, Any]],
    clients_by_id: dict[str, dict[str, Any]],
    legacy_owners: dict[str, dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Propriétaire d'un site — **une seule source de vérité**.

    La fiche ``sites`` fait foi dès qu'elle existe (``client_id`` vide = site non
    attribué, même si un compte garde un ``tenant_id`` périmé). Le repli historique
    ``waas_users.tenant_id`` ne vaut que pour un site **sans fiche**.
    """
    if record:
        return clients_by_id.get(str(record.get("client_id") or ""))
    return legacy_owners.get(site_id)


def legacy_owner_map(clients: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Site -> compte via l'ancien rattachement ``tenant_id`` (sites sans fiche)."""
    owners: dict[str, dict[str, Any]] = {}
    for client in clients:
        if client.get("tenant_id"):
            owners.setdefault(str(client["tenant_id"]), client)
    return owners


def build_catalog(
    records: Iterable[dict[str, Any]],
    inventory: Iterable[dict[str, Any]],
    clients: Iterable[dict[str, Any]],
    live_by_tenant: dict[str, int],
    url_for: Callable[[Optional[dict[str, Any]], Optional[str]], Optional[str]],
) -> list[dict[str, Any]]:
    """Un élément par site connu (fiche et/ou conteneur), trié par nom.

    * fiche sans conteneur : ``status = "not_provisioned"`` (serveur à déployer) ;
    * conteneur sans fiche : repris tel quel, nom = identifiant (site historique).
    """
    clients = list(clients)
    clients_by_id = {str(c.get("id")): c for c in clients if c.get("id")}
    legacy_owners = legacy_owner_map(clients)
    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        site_id = str(record.get("id") or record.get("site_id") or "")
        if site_id:
            by_id[site_id] = {"record": record, "container": None}
    for container in inventory:
        site_id = str(container.get("site_id") or "")
        if site_id:
            by_id.setdefault(site_id, {"record": None, "container": None})["container"] = container

    catalog = [
        catalog_entry(site_id, parts["record"], parts["container"],
                      site_owner(site_id, parts["record"], clients_by_id, legacy_owners),
                      live_by_tenant.get(site_id, 0), url_for)
        for site_id, parts in by_id.items()
    ]
    catalog.sort(key=lambda item: (item["name"] or item["site_id"]).lower())
    return catalog


def catalog_entry(
    site_id: str,
    record: Optional[dict[str, Any]],
    container: Optional[dict[str, Any]],
    owner: Optional[dict[str, Any]],
    live: int,
    url_for: Callable[[Optional[dict[str, Any]], Optional[str]], Optional[str]],
) -> dict[str, Any]:
    record = record or {}
    container = container or {}
    plan = PLANS_BY_ID.get(str(record.get("plan_id") or ""), None)
    port = container.get("port")
    status = container.get("status") or "not_provisioned"
    return {
        "site_id": site_id,
        "name": record.get("name") or site_id,
        "has_record": bool(record),
        "domain": record.get("domain") or "",
        "url": url_for(record, port) if (port or record.get("domain")) else None,
        "client_id": (owner or {}).get("id") or (record.get("client_id") if record else None),
        "client_name": (owner or {}).get("username") or "",
        "plan_id": record.get("plan_id") or "none",
        "plan_name": record.get("plan_name") or (plan or {}).get("name") or "Sans formule",
        "price_monthly": as_number(record.get("price_monthly"), 0.0),
        "contract_status": record.get("contract_status") or "active",
        "contract_since": record.get("contract_since") or None,
        "domain_expires_at": record.get("domain_expires_at"),
        "ssl_expires_at": record.get("ssl_expires_at"),
        "notes": record.get("notes") or "",
        "status": status,
        "running": bool(container.get("running")),
        "health": container.get("health"),
        "port": port,
        "container_id": container.get("container_id"),
        "container_name": container.get("name") or f"waas-{site_id}",
        "started_at": container.get("started_at"),
        "started_at_epoch": container.get("started_at_epoch"),
        "restart_count": container.get("restart_count") or 0,
        "exit_code": container.get("exit_code"),
        "image": container.get("image"),
        "repo_url": record.get("repo_url") or container.get("repo_url"),
        "cpu_percent": container.get("cpu_percent"),
        "memory_mb": container.get("memory_mb"),
        "memory_limit_mb": container.get("memory_limit_mb"),
        "disk_mb": container.get("disk_mb"),
        "pids": container.get("pids"),
        "live_visitors": int(live or 0),
        "created_at": record.get("created_at") or container.get("created_at"),
    }


# ---------------------------------------------------------------------------
# Indicateurs financiers et d'exploitation
# ---------------------------------------------------------------------------

def business_kpis(catalog: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """MRR / ARR / ARPU calculés sur les contrats actifs des fiches ``sites``."""
    sites = list(catalog)
    active = [s for s in sites if s.get("has_record") and s.get("contract_status") == "active"]
    paying = [s for s in active if as_number(s.get("price_monthly")) > 0]
    mrr = round(sum(as_number(s.get("price_monthly")) for s in active), 2)
    plan_counts: dict[str, int] = {}
    for site in active:
        plan_counts[site.get("plan_name") or "Sans formule"] = plan_counts.get(site.get("plan_name") or "Sans formule", 0) + 1
    return {
        "mrr": mrr,
        "arr": round(mrr * 12, 2),
        "arpu": round(mrr / len(paying), 2) if paying else 0.0,
        "active_contracts": len(active),
        "paying_sites": len(paying),
        "plan_counts": plan_counts,
    }


def fleet_counts(catalog: Iterable[dict[str, Any]]) -> dict[str, int]:
    sites = list(catalog)
    return {
        "total": len(sites),
        "online": sum(1 for s in sites if s.get("running")),
        "stopped": sum(1 for s in sites if s.get("status") in ("stopped", "paused")),
        "error": sum(1 for s in sites if s.get("status") in ("error", "restarting")),
        "not_provisioned": sum(1 for s in sites if s.get("status") == "not_provisioned"),
    }


def invoice_totals(invoices: Iterable[dict[str, Any]]) -> dict[str, Any]:
    items = list(invoices)
    paid = [i for i in items if i.get("status") == "paid"]
    unpaid = [i for i in items if i.get("status") in UNPAID_STATUSES]
    return {
        "count": len(items),
        "paid_count": len(paid),
        "paid_total": round(sum(as_number(i.get("amount")) for i in paid), 2),
        "pending_count": len(unpaid),
        "pending_total": round(sum(as_number(i.get("amount")) for i in unpaid), 2),
        "overdue_count": sum(1 for i in items if i.get("status") == "overdue"),
    }


def flag_overdue(invoices: Iterable[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """Présente « en retard » une facture en attente dont l'échéance est passée."""
    shaped = []
    for invoice in invoices:
        due = parse_date(invoice.get("due_at"))
        status = invoice.get("status")
        if status == "pending" and due and due < today:
            status = "overdue"
        shaped.append({**invoice, "status": status})
    return shaped


# ---------------------------------------------------------------------------
# Support
# ---------------------------------------------------------------------------

def shape_ticket(ticket: dict[str, Any], site_names: dict[str, str], reader_role: str = "agency",
                 with_messages: bool = False) -> dict[str, Any]:
    messages = list(ticket.get("messages") or [])
    last = messages[-1] if messages else {}
    other = "client" if reader_role == "agency" else "agency"
    body = str(last.get("body") or "").strip()
    shaped = {
        key: ticket.get(key)
        for key in ("id", "site_id", "subject", "status", "priority", "created_at", "updated_at")
    }
    shaped.update({
        "site_name": site_names.get(str(ticket.get("site_id") or ""), ticket.get("site_id") or "Général"),
        "message_count": len(messages),
        "snippet": body if len(body) <= 120 else f"{body[:119]}…",
        "last_from": last.get("from_role"),
        "unread": sum(1 for m in messages if m.get("from_role") == other and not m.get("seen_at")),
    })
    if with_messages:
        shaped["messages"] = [
            {key: m.get(key) for key in ("id", "from_role", "author_name", "body", "sent_at", "seen_at")}
            for m in messages
        ]
    return shaped


# ---------------------------------------------------------------------------
# Renouvellements (port de OldDashBoard/services/renewal_service.py)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Renewal:
    """Une échéance. ``due=None`` : non datée (automatique ou date à renseigner)."""
    kind: str                 # subscription | domain | ssl | invoice
    site_id: str
    site_name: str
    label: str
    detail: str = ""
    due: Optional[date] = None
    amount: Optional[float] = None
    auto: bool = False

    def days_left(self, today: date) -> Optional[int]:
        return (self.due - today).days if self.due else None

    def state(self, today: date) -> str:
        days = self.days_left(today)
        if days is None:
            return "auto" if self.auto else "unknown"
        if days < 0:
            return "overdue"
        return "soon" if days <= SOON_DAYS else "upcoming"

    def as_dict(self, today: date) -> dict[str, Any]:
        data = asdict(self)
        data["due"] = self.due.isoformat() if self.due else None
        data["days_left"] = self.days_left(today)
        data["state"] = self.state(today)
        return data


_KIND_ORDER = {"invoice": 0, "subscription": 1, "domain": 2, "ssl": 3}


def next_monthly(since: date, today: date) -> date:
    """Prochaine date anniversaire mensuelle ≥ today (jour borné à la fin des mois courts)."""
    if since >= today:
        return since
    year, month = today.year, today.month
    while True:
        day = min(since.day, calendar.monthrange(year, month)[1])
        candidate = date(year, month, day)
        if candidate >= today:
            return candidate
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def _is_managed(domain: str, base_domain: str) -> bool:
    name = domain.lower().rstrip(".")
    base = (base_domain or "").lower().strip(".")
    return bool(base) and (name == base or name.endswith(f".{base}"))


def _site_renewals(site: dict[str, Any], today: date, base_domain: str) -> list[Renewal]:
    renewals: list[Renewal] = []
    name = site.get("name") or site.get("site_id")
    price = as_number(site.get("price_monthly"))
    if site.get("contract_status") == "active" and price > 0:
        since = parse_date(site.get("contract_since")) or parse_date(site.get("created_at"))
        renewals.append(Renewal("subscription", site["site_id"], name, site.get("plan_name") or "Formule",
                                detail="Prélèvement mensuel", due=next_monthly(since, today) if since else None,
                                amount=price))

    domain_name = site.get("domain") or url_host(site.get("url") or "")
    domain_due = parse_date(site.get("domain_expires_at"))
    managed = bool(domain_name) and _is_managed(domain_name, base_domain)
    renewals.append(Renewal("domain", site["site_id"], name, domain_name or "Domaine non renseigné",
                            detail="Sous-domaine géré" if managed else "Registrar à renseigner",
                            due=domain_due, auto=managed and domain_due is None))

    ssl_due = parse_date(site.get("ssl_expires_at"))
    renewals.append(Renewal("ssl", site["site_id"], name, "Certificat SSL",
                            detail="Reverse-proxy de la plateforme", due=ssl_due, auto=ssl_due is None))
    return renewals


def collect_renewals(sites: Iterable[dict[str, Any]], invoices: Iterable[dict[str, Any]], today: date,
                     base_domain: str) -> list[dict[str, Any]]:
    """Échéances datées (par date), puis à renseigner, puis automatiques."""
    site_list = [s for s in sites if s.get("has_record")]
    names = {s["site_id"]: s.get("name") or s["site_id"] for s in site_list}
    renewals = [r for s in site_list for r in _site_renewals(s, today, base_domain)]
    for invoice in invoices:
        if invoice.get("status") not in UNPAID_STATUSES:
            continue
        site_id = str(invoice.get("site_id") or "")
        renewals.append(Renewal(
            "invoice", site_id, names.get(site_id) or invoice.get("client_name") or "—",
            str(invoice.get("number") or "Facture"), detail=invoice.get("client_name") or "",
            due=parse_date(invoice.get("due_at")) or parse_date(invoice.get("issued_at")),
            amount=as_number(invoice.get("amount")),
        ))

    def sort_key(renewal: Renewal) -> tuple:
        kind = _KIND_ORDER.get(renewal.kind, 9)
        if renewal.due is not None:
            return (0, renewal.due, kind, renewal.site_name.lower())
        return (1, 1 if renewal.auto else 0, kind, renewal.site_name.lower())

    return [r.as_dict(today) for r in sorted(renewals, key=sort_key)]


def renewal_kpis(renewals: list[dict[str, Any]]) -> dict[str, Any]:
    dated = [r for r in renewals if r.get("due")]
    upcoming = [r for r in dated if r["state"] != "overdue"]
    urgent = [r for r in dated if r["state"] in ("overdue", "soon")]
    subscriptions = [r for r in renewals if r["kind"] == "subscription"]
    return {
        "next": upcoming[0] if upcoming else None,
        "urgent": len(urgent),
        "overdue": sum(1 for r in urgent if r["state"] == "overdue"),
        "subscriptions": len(subscriptions),
        "monthly": round(sum(as_number(r.get("amount")) for r in subscriptions), 2),
        "automatic": sum(1 for r in renewals if r["state"] == "auto"),
        "unknown": sum(1 for r in renewals if r["state"] == "unknown"),
    }


# ---------------------------------------------------------------------------
# Incidents (port de health_service.check_system_incidents)
# ---------------------------------------------------------------------------

def _incident(ident: str, level: str, icon: str, title: str, message: str, subsystem: str,
              recommendation: str, href: Optional[str] = None, action: Optional[str] = None) -> dict[str, Any]:
    return {"id": ident, "level": level, "icon": icon, "title": title, "message": message,
            "subsystem": subsystem, "recommendation": recommendation, "href": href, "action": action}


def _threshold(value: Any, warn: float, danger: float) -> Optional[str]:
    if value is None:
        return None
    number = as_number(value, -1)
    if number >= danger:
        return "danger"
    return "warn" if number >= warn else None


def build_incidents(host: dict[str, Any], health: dict[str, Any], catalog: Iterable[dict[str, Any]],
                    schema_missing: Iterable[str] = (), security_warnings: Iterable[str] = ()) -> list[dict[str, Any]]:
    incidents: list[dict[str, Any]] = []
    host = host or {}

    level = _threshold(host.get("cpu_percent"), 80, 90)
    if level:
        incidents.append(_incident(
            f"cpu_{level}", level, "cpu", f"Charge CPU {'critique' if level == 'danger' else 'élevée'} "
            f"({host['cpu_percent']} %)", "Le processeur de l'hôte est fortement sollicité.",
            "Processeur hôte", "Identifiez les conteneurs les plus gourmands sur la page Serveur VPS.",
            "/admin/vps", "Voir la flotte"))
    level = _threshold(host.get("ram_percent"), 80, 90)
    if level:
        incidents.append(_incident(
            f"ram_{level}", level, "memory", f"Mémoire vive {'saturée' if level == 'danger' else 'élevée'} "
            f"({host['ram_percent']} %)", f"Il reste {host.get('ram_available_gb', '—')} Go disponibles.",
            "Mémoire hôte", "Arrêtez les vitrines inutilisées ou augmentez la RAM du serveur.",
            "/admin/vps", "Voir la flotte"))
    level = _threshold(host.get("disk_percent"), 85, 92)
    if level:
        incidents.append(_incident(
            f"disk_{level}", level, "hard-drive", f"Espace disque {'critique' if level == 'danger' else 'restreint'} "
            f"({host['disk_percent']} %)", f"Espace libre : {host.get('disk_free_gb', '—')} Go.",
            "Stockage hôte", "Purgez les images Docker inutilisées (docker image prune) et les journaux anciens."))

    infra = (health or {}).get("infrastructure") or {}
    database = infra.get("database") or {}
    if (health or {}).get("cached_at") and database.get("status") not in (None, "UP"):
        incidents.append(_incident(
            "db_down", "danger", "database", "Base Supabase injoignable",
            "L'API ne parvient plus à lire la base de données.", "Supabase",
            "Vérifiez SUPABASE_URL, la clé secrète et l'état du projet Supabase."))
    elif as_number(database.get("latency_ms"), 0) > 1500:
        incidents.append(_incident(
            "db_latency", "warn", "database", f"Latence base élevée ({database['latency_ms']} ms)",
            "Supabase répond lentement.", "Supabase", "Vérifiez la région du projet et la charge des requêtes."))
    docker_check = infra.get("docker_engine") or {}
    if (health or {}).get("cached_at") and docker_check.get("status") not in (None, "UP"):
        incidents.append(_incident(
            "docker_down", "danger", "server", "Moteur Docker indisponible",
            "Les conteneurs des vitrines ne peuvent plus être pilotés.", "Docker",
            "Démarrez le démon Docker sur l'hôte puis rechargez cette page."))

    sites = list(catalog)
    crashed = [s for s in sites if s.get("status") in ("restarting", "error")]
    if crashed:
        names = ", ".join(s["site_id"] for s in crashed[:3])
        incidents.append(_incident(
            "container_crashed", "danger", "server", f"{len(crashed)} conteneur(s) en échec",
            f"Conteneur(s) en erreur ou en crashloop : {names}.", "Flotte VPS",
            "Consultez les journaux du conteneur puis relancez-le.", "/admin/vps", "Gérer la flotte"))
    unreachable = [
        a.get("tenant_id") for a in ((health or {}).get("fleet") or {}).get("alerts") or []
        if a.get("level") == "WARNING"
    ]
    if unreachable:
        incidents.append(_incident(
            "http_unreachable", "warn", "globe", f"{len(unreachable)} vitrine(s) sans réponse HTTP",
            f"Conteneur actif mais page d'accueil injoignable : {', '.join(map(str, unreachable[:3]))}.",
            "Vitrines", "Redémarrez la vitrine ; si le problème persiste, reconstruisez-la.", "/admin/vps",
            "Voir la flotte"))
    pending = [s for s in sites if s.get("status") == "not_provisioned"]
    if pending:
        incidents.append(_incident(
            "sites_unprovisioned", "info", "rocket", f"{len(pending)} site(s) sans serveur",
            "Fiche commerciale créée, conteneur non déployé.", "Configuration flotte",
            "Déployez le serveur depuis la fiche du site.", "/admin/sites", "Voir les sites"))

    failing = ((health or {}).get("external_services") or {}).get("failing") or []
    if failing:
        incidents.append(_incident(
            "monitors_failing", "warn", "activity", f"{len(failing)} sonde(s) externe(s) en échec",
            ", ".join(str(f.get("name") or f.get("target")) for f in failing[:3]) + ".",
            "Sondes externes", "Vérifiez les services tiers concernés depuis la page Santé.", "/admin/health",
            "Ouvrir Santé"))

    missing = list(schema_missing)
    if missing:
        incidents.append(_incident(
            "schema_missing", "info", "database", "Migration Supabase à appliquer",
            f"Tables absentes : {', '.join(missing)}. Facturation, support et historique restent vides.",
            "Supabase", "Exécutez End/migrations/001_cockpit.sql dans l'éditeur SQL de Supabase.",
            "/admin/settings", "Ouvrir les paramètres"))
    for index, warning in enumerate(security_warnings):
        incidents.append(_incident(f"security_{index}", "warn", "shield-check", "Configuration de sécurité",
                                   warning, "Sécurité", "Définissez le secret dans l'environnement puis redémarrez l'API.",
                                   "/admin/settings", "Ouvrir les paramètres"))

    rank = {"danger": 0, "warn": 1, "info": 2}
    incidents.sort(key=lambda item: rank.get(item["level"], 3))
    return incidents


def alerts_summary(incidents: list[dict[str, Any]]) -> dict[str, Any]:
    """Carte « Alertes » de la sidebar : incident le plus grave (info exclue du ton)."""
    actionable = [i for i in incidents if i["level"] in ("danger", "warn")]
    if not actionable:
        return {"tone": "good", "title": "Alertes", "text": "Tout est nominal", "count": 0,
                "message": "Infrastructure et vitrines opérationnelles. Aucun incident en cours."}
    primary = actionable[0]
    extra = len(actionable) - 1
    return {
        "tone": "danger" if primary["level"] == "danger" else "warn",
        "title": "Incident critique" if primary["level"] == "danger" else "Alertes",
        "text": primary["title"] + (f" (+{extra})" if extra else ""),
        "count": len(actionable),
        "message": f"{primary['message']} {primary['recommendation']}".strip(),
    }


def today_utc() -> date:
    return datetime.now(timezone.utc).date()
