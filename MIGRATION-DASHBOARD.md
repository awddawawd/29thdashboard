# Port de l'ancien dashboard (OldDashBoard) vers l'architecture Front/ + API

État : **porté**. Ce document décrit l'architecture retenue, la correspondance
écran par écran et les éléments de l'ancien dashboard qui n'ont pas d'équivalent.

---

## 1. Règles de structure

### 1.1 Une adresse = un dossier = un `index.html`

L'API résout l'adresse demandée dans l'arborescence de `Front/` :

| Adresse | Fichier servi | Garde |
| --- | --- | --- |
| `/admin/<section>[/<sous-section>]` | `Front/admin/<section>/…/index.html` | `require_admin` (404 sinon) |
| `/portal[/<section>]` | `Front/portal/…/index.html` | session valide (401 → `/login`) |
| `/studio` | `Front/studio/index.html` | session valide |
| `/login`, `/status` | `Front/login`, `Front/status` | public |

Les identifiants passent en paramètre de requête (`/admin/sites/detail?id=…`,
`/portal/billing?site=…`) : pas de dossier par identifiant.
Le JavaScript propre à une page est dans une balise `<script>` de son `index.html`.

### 1.2 Rien de commun n'est dupliqué

| Élément commun | Emplacement unique |
| --- | --- |
| Tête HTML (métas, auteur, favicon, polices, 15 feuilles CSS, JSON-LD) | `Front/components/head.html` |
| Commentaire d'attribution, lien d'évitement, état de chargement | `Front/components/body-start.html` (+ `attribution.html`) |
| Squelettes de chargement (une brique par widget) | `Front/components/skeleton/*.html` |
| Sidebar / topbar du cockpit | `Front/components/admin/*.html` |
| Sidebar / topbar de l'espace client | `Front/components/portal/*.html` |
| Page d'erreur 404/5xx | `Front/components/error.html` |
| Scripts communs | `Front/components/scripts.html` → `Front/js/*.js` |
| Styles | `Front/css/01-tokens.css` … `15-app.css` |

`Front/components` n'est jamais servi directement ; l'ancien `cockpit.bundle.css`
(copie intégrale des feuilles 01-14) et l'ancien `components/sprite.html` (copie
des icônes de `cockpit.js`) ont été supprimés.

### 1.3 Inclusions avec paramètres

```html
<!-- @include body-start.html brand="Cockpit WaaS" -->
<!-- @include admin/topbar.html title="Sites & vitrines" -->
```

Les `{{cle}}` du fragment sont remplacés par la valeur (échappée HTML). Résolution
confinée à `Front/components` (`End/pages.py`, partagé par l'API et `run.py`).
`{{v}}` est l'empreinte des CSS/JS : les URL changent à chaque mise à jour, le cache
navigateur n'est jamais périmé.

### 1.4 Scripts communs (`Front/js`)

| Fichier | Rôle |
| --- | --- |
| `cockpit.js` | Noyau du DS : icônes (sprite injecté), toasts, dialogues, menus, onglets, horloge, tiroir mobile |
| `charts.js` | Graphiques SVG : courbes (spline monotone), disponibilité 90 j, jauges, anneau, classement, mini-séries |
| `ui.js` | Briques DOM (`UI.kpi`, `UI.card`, `UI.table`, `UI.statusBadge`, `UI.dialog`, `UI.confirm`…) — jamais d'`innerHTML` sur des données |
| `app.js` | Client API, écran de chargement (`App.boot`), coques cockpit/portail, rafraîchissement suspendu en arrière-plan |
| `views.js` | Rendus métier partagés : tableaux sites/factures/échanges, reçu imprimable, messagerie, disponibilité |

### 1.5 Écran de chargement

Chaque page s'ouvre sous un écran de chargement plein écran (marque, barre fine
indéterminée, étape en cours). `App.boot()` le retire quand la coque **et** les
données de la page sont arrivées, puis le contenu apparaît par une montée
échelonnée unique. Les anciennes cartes « à ombre pulsante » et le contournement
du chargeur par `sessionStorage` ont été retirés. Filets : message d'erreur et
bouton *Réessayer* au-delà de 15 s ; retrait CSS automatique à 20 s si le
JavaScript ne démarre pas.

---

## 2. Correspondance des écrans

| Ancien (FastHTML) | Nouveau | API |
| --- | --- | --- |
| `views/overview.py` | `/admin/dashboard` | `GET /admin/api/overview` |
| `views/sites.py` (+ dialogue de création) | `/admin/sites` (`?new=1` ouvre le dialogue) | `GET /admin/api/sites`, `POST /admin/api/sites/deploy` |
| `views/site_detail.py` | `/admin/sites/detail?id=` | `GET/PATCH /admin/api/sites/{id}`, sondes `…/monitors` |
| `views/servers.py` | `/admin/vps` | `GET /admin/api/sites`, `POST …/{id}/action` |
| `views/vps_detail.py` (console, journaux, disponibilité) | `/admin/vps/detail?id=` | `…/{id}/exec`, `…/{id}/logs`, `…/{id}/probe`, `DELETE …/{id}?purge_record=true` |
| gestion des comptes | `/admin/clients` | `GET/POST /admin/api/clients`, `PATCH`, `…/password`, `DELETE` |
| `views/billing.py` | `/admin/billing` | `GET/POST /admin/api/invoices`, `…/pay`, `…/generate` |
| `views/renewals.py` | `/admin/renewals` | `GET /admin/api/renewals` |
| `views/tickets.py` | `/admin/support?ticket=&filter=` | `GET /admin/api/tickets`, `…/{id}`, `…/messages`, `…/status` |
| `views/health.py` | `/admin/health` | `GET /admin/api/health/overview`, `…/host`, `…/timeline`, `POST …/probe` |
| `views/settings.py` | `/admin/settings` | `GET /admin/api/platform`, `POST /admin/api/fleet/settings` |
| `views/portal.py` (3 onglets) | `/portal`, `/portal/billing`, `/portal/support` | `GET /api/v1/portal/overview`, `/api/v1/sites/{id}/invoices`, `…/tickets`, `/api/v1/tickets/{id}` |
| `views/studio.py` + `studio_bridge.js` | `/studio?site=` | `/api/v1/sites/{id}/content`, `…/content/batch`, `…/upload` |
| `views/status_page.py` | `/status` | `GET /api/status` |
| `views/auth.py` | `/login` | `POST /api/login` |

Coque commune du cockpit : `GET /admin/api/shell` (profil, environnement, alertes,
tickets ouverts, audience en direct), rafraîchie toutes les 30 s.

---

## 3. Données

`End/migrations/001_cockpit.sql` ajoute `sites`, `invoices`, `tickets`,
`ticket_messages`, `uptime_daily`, `host_metrics_hourly` et les fonctions
`create_waas_user` / `set_waas_user_password` (bcrypt, comme l'existant). RLS
activée sans politique : seule la clé secrète de l'API y accède.

Sans migration, les lectures renvoient des listes vides (bandeau « Migration
requise ») et les écritures répondent 503 avec un message actionnable.

Mesures en direct (sans stockage brut) : visiteurs « en direct » = vus depuis
moins de 5 min dans `tenant_analytics`, lue de façon incrémentale ; disponibilité
agrégée par jour et par service ; charge de l'hôte par minute (mémoire) et par
heure (Supabase). `psutil` (ajouté au `pyproject`) fournit CPU/RAM/réseau ;
sans lui, seules les mesures système disponibles sont affichées (jamais de valeur
inventée).

### 3.1 Règles de sécurité à connaître

- **Propriétaire d'un site** : la fiche `sites.client_id` fait foi. L'ancien
  `waas_users.tenant_id` n'est retenu que pour un site **sans fiche** (et il est
  effacé quand un site change de propriétaire ou est purgé). Les anciennes routes
  `/waas/my-site/*` appliquent la même règle.
- **Portail client** : les notes internes, le dépôt source, le registrar et les
  factures en brouillon ne sont jamais envoyés au client. Les réponses de l'agence
  sont signées avec le réglage `agency_name` (jamais l'identifiant d'un admin).
- **CORS** : avec cookies, seul le serveur de développement `run.py`
  (`localhost:3000`) est autorisé. La collecte d'audience
  (`/waas/analytics/hit/<site>`) est la seule route ouverte à toutes les origines,
  pour que les vitrines sur leur propre domaine puissent l'appeler.
- **CSRF en local** : `localhost` et `127.0.0.1` ne sont équivalents que **sur le
  même port** (une vitrine locale sur un autre port est refusée).
- **Facturation mensuelle** : chaque ligne générée porte
  `{"kind": "subscription", "period": "AAAA-MM"}` ; relancer la génération ne
  refacture jamais le même mois.
- **Console** : 4 commandes simultanées au maximum (au-delà : 429), 10 s puis
  arrêt forcé.
- **Reconstruire** : l'ancien conteneur n'est supprimé qu'une fois le nouveau
  créé ; en cas d'échec, il est restauré et relancé.

---

## 4. Non portés (aucun équivalent dans la nouvelle architecture)

| Ancien élément | Raison |
| --- | --- |
| Agent IA d'astreinte (Réglages) | dépend du paquet `agent/` absent du dépôt |
| Éditeur « Site du studio » (`/cockpit/vitrine`) | dépend de `database/vitrine` absent du dépôt |
| Dépôt ZIP / dossier, Git auto-sync, rollback de sauvegarde | les vitrines se déploient désormais depuis un dépôt Git (`REPO_URL`) ; remplacé par *Reconstruire* et *Resynchroniser* |
| Snapshots du Studio | l'éditeur conteneur ne produit plus de snapshots ; le contenu vit dans `tenant_content` |
| Grille de widgets personnalisable (GridStack) de l'espace client | dépend de `library/js/grid.js` absent ; remplacée par une composition fixe |
| Thème « verre » et teintes | feuilles 15/16-glass absentes du nouveau DS ; DA claire conservée |
| Paiement par carte côté client | aucun prestataire de paiement : l'agence marque les factures payées |
