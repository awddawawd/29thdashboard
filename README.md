# WaaS — Fleet Manager

Plateforme de gestion de flotte de vitrines web (WaaS) : API FastAPI, worker de
télémétrie, portail client et console d'administration.

## Arborescence

| Chemin | Rôle |
| --- | --- |
| `End/main.py` | API FastAPI (auth JWT/RBAC, cockpit agence, espace client, pages) |
| `End/cockpit.py` | Logique métier pure du cockpit (catalogue, MRR, échéances, incidents) |
| `End/telemetry.py` | Moteur de télémétrie (Docker, hôte, trafic, disponibilité 90 j, sondes) |
| `End/db.py` | Accès Supabase (comptes, sites, factures, support, contenu, supervision) |
| `End/git_source.py` | Dépôts GitHub privés : vérification d'accès et relais de l'archive aux conteneurs |
| `End/pages.py` | Rendu des pages : inclusions `<!-- @include … -->` de `Front/components` |
| `End/migrations/` | Migrations SQL Supabase (à exécuter dans l'éditeur SQL) |
| `End/content_sync.py` | Réconciliation du contenu éditable avec le gabarit (logique pure, voir « Éditeur visuel ») |
| `End/extractor.py` | Prototype d'extraction **obsolète, importé nulle part** (l'extraction vit dans `waas-base-image/waas_content.py`) |
| `waas-base-image/` | Image des vitrines : clonage, `sync_engine.py` (synchronisation), `waas_content.py` (modèle de contenu), `editor/` (pont d'édition du Studio) |
| `Front/` | Pages (1 dossier = 1 `index.html` = 1 adresse), CSS et JS communs ; le Studio a ses modules dans `Front/js/studio/` et sa feuille `Front/css/16-studio.css` |
| `tests/` | Tests de non-régression et de durcissement, banc d'essai du Studio (`studio_harness.py`) |

## Démarrage

```bash
uv sync
python End/main.py            # ou : uvicorn main:app --app-dir End
```

Première installation : exécuter `End/migrations/001_cockpit.sql` dans Supabase
(SQL Editor → Run). Tant qu'elle n'est pas appliquée, le cockpit fonctionne en
mode dégradé : facturation, support et historique restent vides, et un bandeau
l'indique dans les pages concernées et dans **Paramètres**.

## Pages et adresses

Chaque adresse correspond à un dossier de `Front/` contenant son `index.html`
(son JavaScript propre est dans une balise `<script>` de la page). Tout ce qui est
commun n'existe qu'en un exemplaire : fragments dans `Front/components/`, styles
dans `Front/css/` (01 → 15), scripts dans `Front/js/`.

| Adresse | Dossier | Accès |
| --- | --- | --- |
| `/login` | `Front/login` | public |
| `/status` | `Front/status` | public (agrégats, aucun identifiant client) |
| `/admin/dashboard` · `/admin/sites` · `/admin/sites/detail?id=` | `Front/admin/…` | agence (404 sinon) |
| `/admin/vps` · `/admin/vps/detail?id=` · `/admin/health` | `Front/admin/…` | agence |
| `/admin/clients` · `/admin/billing` · `/admin/renewals` · `/admin/support` · `/admin/settings` | `Front/admin/…` | agence |
| `/portal` · `/portal/billing` · `/portal/support` (`?site=`) | `Front/portal/…` | compte connecté (données filtrées par propriété) |
| `/studio?site=` | `Front/studio` | agence ou client propriétaire |

`Front/components` n'est jamais servi : ses fragments sont inclus côté serveur.
`/editor` redirige vers `/studio` ; `End/index.html` (ancien éditeur) n'est plus servi.

## Variables d'environnement

Les valeurs par défaut présentes dans le code ne sont conservées que pour la
compatibilité de déploiement : en production, définir au minimum

| Variable | Rôle |
| --- | --- |
| `SUPABASE_URL` | URL du projet Supabase |
| `SUPABASE_SECRET_KEY` | Clé secrète serveur `sb_secret_...` (ou alias legacy `SUPABASE_SERVICE_KEY`) |
| `SUPABASE_PUBLISHABLE_KEY` | Clé publiable `sb_publishable_...` (ou alias legacy `SUPABASE_ANON_KEY`) |
| `WAAS_SECRET_KEY` | Clé de signature des jetons JWT |
| `WAAS_SYNC_SECRET` | Secret dédié aux jetons de synchronisation des conteneurs (repli : dérivé de `WAAS_SECRET_KEY`) |
| `WAAS_ANALYTICS_PEPPER` | Sel de pseudonymisation des visiteurs |
| `WAAS_GITHUB_TOKEN` | *Facultatif.* Jeton GitHub **en lecture seule** pour héberger des dépôts privés (voir ci-dessous). Ne quitte jamais l'API |

### Dépôts GitHub privés

Sans jeton, seuls les dépôts publics sont déployables (le conteneur fait un
`git clone` anonyme). Pour héberger un dépôt **privé** :

1. Créer un jeton *fine-grained* sur GitHub (Settings → Developer settings →
   Personal access tokens → Fine-grained) : propriétaire = le compte ou
   l'organisation qui héberge les sites, *Repository access* = les dépôts clients
   (ou tous), permission **Contents : Read-only**, avec une date d'expiration.
   Pour une organisation, un compte machine dédié est préférable à un compte personnel.
2. Le définir dans l'environnement de l'API (`WAAS_GITHUB_TOKEN=github_pat_…`) et
   redémarrer l'API. **Paramètres** affiche « Dépôts GitHub privés : Jeton configuré ».
3. Reconstruire l'image (`docker build -t waas-base-image:latest waas-base-image`).
4. Saisir l'URL habituelle (`https://github.com/<org>/<dépôt>.git`) au
   déploiement, ou dans la fiche d'un site non configuré puis **Déployer**.

L'accès est vérifié dès la saisie : un dépôt privé sans jeton, hors de portée du
jeton ou un jeton expiré renvoie une erreur explicite (422) au lieu d'un conteneur
qui redémarre en boucle. Une fois le jeton défini, chaque nouveau conteneur GitHub
démarre en `SOURCE_MODE=api` : il demande l'archive à `GET /internal/sync/source`
avec son `SYNC_TOKEN` et sa clé `SOURCE_NONCE`, et l'API la télécharge avec le
jeton. **Le jeton n'est jamais transmis au conteneur** (ni dans son environnement,
ni dans `.git/config`, ni dans la racine web). Les conteneurs existants gardent
leur mode jusqu'à leur reconstruction ; GitLab et les hôtes personnalisés restent
en `git` (publics). `TEMPLATE_REF` n'est pris en compte qu'en mode `git` : le mode
`api` déploie la branche par défaut. Si le jeton expire, les dépôts publics restent
servis. **Tout le dépôt est publié** : il ne doit contenir que les fichiers du site.
Garanties et limites détaillées : `GUIDE-SECURISATION.md` §1.7.

## Isolation multi-tenant (conteneurs clients)

Un conteneur `waas-tenant` s'exécute dans un environnement **non fiable** : il ne
doit donc détenir aucun secret du plan de contrôle. Le déploiement
(`/admin/api/sites/deploy`) lui transmet uniquement `TENANT_ID`, `REPO_URL`,
`SOURCE_MODE`, `API_BASE_URL`, `BROWSER_API_URL`, `PORTAL_ORIGIN` et un
`SYNC_TOKEN` HMAC **scopé à son seul locataire**.

`API_BASE_URL` est l'URL par laquelle les **conteneurs** joignent l'API ;
`BROWSER_API_URL` celle par laquelle le **navigateur** la joint. Les deux
diffèrent en développement (`http://host.docker.internal:8000` contre
`http://localhost:8000`) et cette confusion rendait muets la collecte d'audience
et l'aperçu encadré du Studio : voir `GUIDE-SECURISATION.md` §1.2b et le réglage
`browser_api_url`.

Le moteur de synchronisation (`waas-base-image/sync_engine.py`) ne parle plus
jamais à Supabase : il utilise les endpoints internes
`GET|POST /internal/sync/content`, et le `tenant_id` est imposé côté serveur à
partir du jeton (jamais lu depuis la requête). Un jeton volé ne donne donc accès
qu'aux données de son propre locataire, et la clé `service_role` reste confinée
au processus API.

### Séquence de mise en service (obligatoire)

La clé `service_role` a été exposée (dépôt Git **et** variables d'environnement
des conteneurs déjà déployés) : la rotation est indispensable.

1. Déployer cette version de l'API (routes internes + déploiement sans clé).
2. Reconstruire l'image (`docker build -t waas-base-image:latest waas-base-image`)
   puis **recréer chaque conteneur locataire** via l'API : les conteneurs
   existants conservent sinon l'ancienne clé dans leur environnement.
3. **Révoquer/régénérer la clé `service_role`** dans le tableau de bord Supabase,
   mettre à jour `SUPABASE_SERVICE_KEY`, redémarrer l'API.
4. Définir `WAAS_SYNC_SECRET` (la rotation de ce secret ou de `WAAS_SECRET_KEY`
   nécessite de recréer les conteneurs locataires).

> ⚠️ **Ordre impératif.** Ne recréez **pas** les conteneurs locataires avant
> d'avoir déployé la nouvelle API : le nouveau moteur de synchronisation exige les
> endpoints `/internal/sync/content`. L'ancienne API ne les expose pas, et le
> conteneur redémarrerait en boucle (contenu non hydraté). Déployez l'API d'abord,
> les conteneurs ensuite.

## Réglages dynamiques (table `fleet_settings`)

| Clé | Défaut | Effet |
| --- | --- | --- |
| `telemetry_probe_port_allowlist` | *vide* | Si renseignée (`80,443,8080`), seuls ces ports sont sondables |
| `telemetry_blocked_probe_ports` | *vide* | Ports supplémentaires interdits (mode denylist) |
| `telemetry_allowed_http_methods` | `GET,HEAD` | Méthodes autorisées pour les sondes HTTP |
| `telemetry_probe_concurrency` | `10` | Sondes externes simultanées |
| `telemetry_container_probe_concurrency` | `20` | Sondes locales simultanées |
| `telemetry_max_monitors_per_collection` | `0` | Plafond de sondes traitées par cycle (0 = illimité) |
| `telemetry_block_self_targeting` | `1` | Interdit de sonder la plateforme elle-même |
| `tenant_port_binding` | `127.0.0.1` | Interface d'écoute des vitrines (`0.0.0.0` = exposition directe, déconseillé) |
| `allowed_git_hosts` | `github.com,gitlab.com` | Hôtes Git autorisés pour `REPO_URL` |

## Durcissement des conteneurs clients

Chaque vitrine tourne désormais dans un conteneur sans privilèges :

| Mesure | Valeur |
| --- | --- |
| Utilisateur | `10001:10001` (jamais root) |
| Capabilities | `cap_drop=["ALL"]` + `no-new-privileges` |
| Système de fichiers | rootfs **lecture seule**, `/tmp` et `/run` en `tmpfs`, contenu dans un volume |
| Ressources | 256 Mo (RAM+swap), 0,5 vCPU, 128 PID, `ulimit` nofile/nproc |
| Journalisation | `json-file` plafonné (10 Mo × 3) |
| Santé | `HEALTHCHECK` HTTP sur `/` (Docker signale « unhealthy ») |
| Réseau | réseau Docker dédié `waas-tenants`, **communication inter-conteneurs désactivée** |
| Exposition | port `8080` lié à `127.0.0.1` (le reverse-proxy de l'hôte termine le TLS) |
| Dépôt Git | jamais copié dans la racine web (`/.git` renvoie 404), fichiers cachés bloqués |
| En-têtes | `nosniff`, `Referrer-Policy`, `frame-ancestors <origine du Studio>`, `limit_req` anti-flood |

> ⚠️ **Conséquence pour l'architecture d'exposition.** Les vitrines n'étant plus
> publiées sur toutes les interfaces, le schéma d'URL `http://<client_base_domain>:<port>`
> n'est valable que derrière un reverse-proxy. Configurez une règle par site
> (`https://<site>.<domaine>` → `http://127.0.0.1:<port>`) et le TLS, puis
> mettez à jour `client_base_domain`. Pour un dépannage transitoire seulement,
> `tenant_port_binding=0.0.0.0` rétablit l'ancien comportement.

> ⚠️ **L'API doit être joignable depuis les conteneurs.** Le déploiement refuse
> (422) une `api_public_url` vide ou en `localhost`/`127.0.0.1` : depuis un
> conteneur, `localhost` désigne le conteneur lui-même, ce qui faisait boucler le
> moteur de synchronisation. Liez l'API sur une interface accessible aux
> conteneurs (`--host 172.17.0.1`) ou exposez-la en HTTPS.

> ⚠️ **Et depuis le navigateur.** Le réglage `browser_api_url` porte cette seconde
> vue, utilisée par l'endpoint du micro-tracker d'audience et par
> `frame-ancestors` (aperçu du Studio). Sans lui, une `api_public_url` en
> `http://host.docker.internal:8000` — pourtant recommandée pour les conteneurs —
> ne résout pas depuis un poste : audience vide et iframe bloquée, sans erreur
> explicite côté API. Détails et vérifications : `GUIDE-SECURISATION.md` §1.2b.

## Éditeur visuel (Studio)

Le Studio (`/studio?site=<id>&page=<fichier>`) affiche la vitrine dans un aperçu
encadré et permet de modifier **tout texte visible** (titres, paragraphes, suite d'un
texte après un `<strong>` ou un `<br>`, boutons à icône, cellules, légendes…), les
images (téléversement ou adresse, texte alternatif), le titre de la page et sa
description pour Google, puis de publier.

### Chaîne complète

1. **Au démarrage du conteneur**, `sync_engine.py` copie le gabarit cloné dans un
   dossier d'origine (tmpfs) puis, pour chaque page, envoie l'inventaire de ses
   emplacements éditables (`waas_content.py`) à `POST /internal/sync/inventory`.
2. **L'API réconcilie** (`End/content_sync.py`) : nouveaux éléments indexés,
   éléments déplacés par une évolution du gabarit suivis grâce à leur texte d'origine,
   contenus devenus orphelins détachés (consultables dans le Studio), et renvoie les
   seules valeurs du client à injecter.
3. **La page publique** est réécrite atomiquement, DOCTYPE et espaces conservés,
   avec de courtes annotations `data-waas="N"`. La carte `/__waas/map/<page>.json` et
   le pont `/__waas/editor-bridge.js` ne sont chargés que dans l'aperçu encadré du
   Studio (`?waas_edit=true`) : un visiteur ne télécharge rien de plus.
4. **Publier** relance cette hydratation à chaud (`docker exec … sync_engine.py
   --rehydrate`) : aucune coupure, aucun reclonage. Le redémarrage du conteneur ne
   sert plus que de repli (image antérieure, exec refusé, conteneur arrêté).

Dans l'aperçu, le mode **Édition** neutralise liens, boutons, formulaires et scripts du
site ; **double-clic** = modifier (curseur au point cliqué, aucun mot sélectionné, reste
de la page assombri : réglage *Subtil* / *Marqué* dans le menu « … »), **Entrée** valide,
**Échap** annule, **Tab** passe au texte suivant, **Ctrl+clic** suit un lien. Le mode
**Aperçu** rend la page interactive (menus, onglets) sans perdre les modifications :
revenir en Édition permet alors de modifier un texte habituellement masqué.
L'enregistrement est protégé contre l'écrasement silencieux (valeur attendue : un texte
modifié entre-temps par quelqu'un d'autre est signalé en conflit, à arbitrer).

### Migration et mise en service

* `End/migrations/002_editor.sql` (facultative, recommandée) : texte d'origine de
  chaque élément (`source_value`, relocalisation quand le gabarit évolue) et audit
  (`updated_at`, `updated_by`). Sans elle, l'éditeur fonctionne par clé XPath, sans
  relocalisation ni audit. Les ancres des enregistrements existants sont complétées au
  démarrage suivant de chaque vitrine.
* Ordre de déploiement : API d'abord (elle comprend les deux protocoles), puis
  reconstruction de l'image (`docker build -t waas-base-image:latest waas-base-image`)
  et recréation des vitrines (fiche du site → Reconstruire). Une vitrine non
  reconstruite garde l'ancien pont : le Studio le détecte et l'indique par un bandeau.
* Auteurs de gabarits : `data-waas-id="hero"` sur un élément donne des clés stables à
  tout son contenu (réorganisations de page sans effet) ; `data-waas-skip` exclut un
  sous-arbre de l'édition (contenu généré par script, mentions légales figées…).

### Limites connues

* Édition de **texte** : la mise en forme (gras, liens) et la structure se modifient
  dans le gabarit. Un texte réécrit au chargement par un script du site (traduction,
  compteur animé) n'est modifiable que depuis le panneau.
* Le texte voisin d'un élément HTML5 vide mal interprété par le parseur (`<wbr>`,
  `<embed>`) reste modifiable depuis le panneau seulement.
* L'aperçu en édition ne reproduit pas les fragments HTML injectés par JavaScript :
  leurs textes sont modifiables depuis le panneau (« Tout le site »).

## Tests

```bash
.venv/Scripts/python.exe tests/test_telemetry_hardening.py
.venv/Scripts/python.exe tests/test_tenant_isolation.py
.venv/Scripts/python.exe tests/test_api_hardening.py
.venv/Scripts/python.exe tests/test_cockpit.py
.venv/Scripts/python.exe tests/test_private_repos.py
.venv/Scripts/python.exe tests/test_editor_content.py
.venv/Scripts/python.exe tests/test_editor_sync.py
```

Banc d'essai du Studio, sans Supabase ni Docker (API réelle, base en mémoire, vrai
moteur, vitrine de démonstration sur une autre origine) :

```bash
.venv/Scripts/python.exe tests/studio_harness.py
```

puis ouvrir http://localhost:8765/harness.

La première suite couvre : matrice anti-SSRF, politique de ports et de méthodes,
épinglage d'IP (anti-DNS-rebinding) et préservation de la validation TLS,
absence de matérialisation des corps de réponse, résolveur DNS auto-réparant,
robustesse des KPIs et collecte best-effort.

La seconde prouve l'isolation locataire : aucun secret partagé dans
l'environnement des conteneurs, jetons HMAC infalsifiables, `tenant_id` imposé
côté serveur (pas d'IDOR) et contrat HTTP du moteur de synchronisation.

La troisième couvre le durcissement de l'API : refus par défaut du contrôle de
propriété, liste blanche des dépôts Git (anti-SSRF), URL d'API joignable,
limiteur de connexion, `/editor` authentifié et en-têtes de sécurité.

La quatrième couvre le cockpit : inclusions confinées et échappées, pages
introuvables hors de leur dossier, page d'erreur sans XSS réfléchie, logique métier
(MRR, échéances, incidents), agrégateurs de télémétrie (trafic incrémental,
disponibilité, historique horaire) et isolation des données du portail client.

La cinquième couvre les dépôts privés : jeton GitHub absent du conteneur, accès
vérifié à la saisie (déploiement, fiche, reconstruction), relais limité au dépôt
du locataire du jeton (redirections hors GitHub refusées, quotas, taille bornée)
et extraction sûre dans le conteneur (traversée de chemin, liens absolus).

La sixième couvre le modèle de contenu de l'éditeur : extraction de tous les textes
visibles, clés historiques toujours valides (moteur v1 archivé dans
`tests/fixtures/`), hydratation sans duplication ni perte d'espaces, DOCTYPE et
encodage d'origine conservés, URL d'image dangereuses refusées, et chaque cas de
réconciliation (gabarit réorganisé, permuté, retouché, éléments orphelins, mode sans
migration).

La septième exécute la chaîne réelle moteur ↔ API ↔ base simulée : isolation des pages
au même XPath, conflits d'édition, publication à chaud idempotente, évolution du
gabarit, mise à niveau d'enregistrements historiques, repli sur une API antérieure,
droits et validation des endpoints, pont d'édition assemblé valide.
