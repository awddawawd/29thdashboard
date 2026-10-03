# Guide de sécurisation — plateforme WaaS

Ce document décrit **tout ce qui reste à faire** après le durcissement du code :
les changements de comportement à intégrer dans l'exploitation, les points laissés
volontairement de côté, et la feuille de route pour considérer la plateforme comme
réellement sécurisée.

Légende : 🔴 bloquant · 🟠 important · 🟡 recommandé · ⚪ optionnel

---

## 0. Vue d'ensemble

| # | Action | Pourquoi | Danger | Effort |
|---|---|---|---|---|
| 0 | Sauvegarder base + volumes | toute migration peut casser | 🔴 | 30 min |
| 1 | Exporter les secrets d'environnement, **révoquer la clé `service_role`** | clé publiée (dépôt + conteneurs) | 🔴 | 1 h |
| 2 | Retirer le repli codé en dur de `db.py` | la clé ne doit pas exister dans le code | 🔴 | 10 min |
| 3 | Lier l'API sur une interface joignable des conteneurs | sinon crashloop des vitrines | 🔴 | 15 min |
| 4 | Reconstruire l'image + recréer les conteneurs | retire la clé du runtime client, applique le durcissement | 🔴 | 1 h |
| 5 | Reverse-proxy + TLS par site (port 8080 lié à `127.0.0.1`) | les vitrines ne sont plus exposées directement | 🔴 | 2 h |
| 6 | Rôle Postgres à privilèges minimaux + RLS | la vraie parade à la clé d'administration | 🟠 | 3 h |
| 7 | Proxy de socket Docker | l'API a aujourd'hui un accès root-équivalent | 🟠 | 2 h |
| 8 | Audit des actions admin + alertes | sans trace, pas de détection | 🟡 | 2 h |
| 9 | CI : build épinglé + scan de vulnérabilités | supply chain | 🟡 | 3 h |
| 10 | Hygiène continue (rotation, revue, sauvegardes testées) | la sécurité est un processus | 🟡 | continu |

**Ne sautez pas l'étape 1** : les étapes suivantes perdent l'essentiel de leur
valeur tant que la clé compromise est active.

---

## 1. Changements de comportement à intégrer

### 1.1 🔴 Les vitrines ne sont plus exposées sur `0.0.0.0`

**Avant** : `ports={"80/tcp": None}` → `0.0.0.0:56697 → 80/tcp`, joignable depuis
Internet en HTTP clair, sur un port aléatoire.
**Après** : `ports={"8080/tcp": ("127.0.0.1", None)}` → joignable **uniquement**
depuis l'hôte.

**Conséquence** : le schéma d'URL `http://<client_base_domain>:<port>` ne
fonctionne plus depuis l'extérieur. Il faut un reverse-proxy.

À faire :

1. **Un fichier de configuration par site**, généré au déploiement (approche
   recommandée, aucune table de correspondance dynamique) :
   ```nginx
   # /etc/nginx/conf.d/sites/boutique-dupont.conf  (généré par votre script de déploiement)
   server {
       listen 443 ssl http2;
       server_name boutique-dupont.sites.exemple.com;

       ssl_certificate     /etc/letsencrypt/live/sites.exemple.com/fullchain.pem;
       ssl_certificate_key /etc/letsencrypt/live/sites.exemple.com/privkey.pem;
       add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

       location / {
           proxy_pass http://127.0.0.1:56697;      # port renvoyé par /admin/api/sites
           proxy_set_header Host $host;
           proxy_set_header X-Forwarded-Proto https;
           proxy_set_header X-Forwarded-For $remote_addr;
       }
   }
   ```
   Puis `nginx -t && nginx -s reload` après chaque déploiement
   (`POST /admin/api/sites/deploy` renvoie déjà `host_port`, ce qui permet de
   générer le fichier automatiquement).
2. Mettre `client_base_domain` = `sites.exemple.com` (ou votre domaine).
3. Certificat **wildcard** (`*.sites.exemple.com`) ou un certificat par site.

**Repli de dépannage** : `tenant_port_binding = 0.0.0.0` dans `fleet_settings`
rétablit l'ancien comportement (avertissement journalisé à chaque collecte).
À n'utiliser que le temps de mettre le proxy en place.

**Vérification** : `docker port <conteneur>` doit montrer `127.0.0.1:<port>` et
`https://<site>.<domaine>` doit répondre.

### 1.2 🔴 L'API doit être joignable depuis les conteneurs

**Avant** : `API_BASE_URL=http://localhost:8000` ; dans un conteneur, `localhost`
désigne le conteneur → l'API était injoignable.
**Après** : le déploiement **refuse** (HTTP 422) une `api_public_url` vide ou en
`localhost` / `127.0.0.1`, avec un message explicite.

À faire (au choix) :

* **Public HTTPS** (recommandé) : `api_public_url = https://api.exemple.com`.
* **Interne au pont Docker** : `uvicorn main:app --host 172.17.0.1 --port 8000`
  puis `api_public_url = http://172.17.0.1:8000`.
  ⚠️ Cela expose l'API à **tous les conteneurs du pont** : à combiner avec le
  réseau dédié (déjà créé) et le rôle Postgres restreint (§2.2).
* **Via `host.docker.internal`** : le déploiement ajoute déjà l'entrée
  `host-gateway` ; utilisez `http://host.docker.internal:8000` (l'API doit être
  liée sur une interface joignable, pas seulement `127.0.0.1`).
  ⚠️ **`host.docker.internal` ne résout que depuis un conteneur** : il ne doit
  jamais servir au navigateur. Deux consommateurs côté navigateur existent, et
  c'est le rôle du réglage **`browser_api_url`** (§1.2b) :
  l'endpoint du micro-tracker d'audience et le `frame-ancestors` de l'aperçu du
  Studio. En local : `api_public_url = http://host.docker.internal:8000` **et**
  `browser_api_url = http://localhost:8000`.

### 1.2b 🔴 L'API telle que le **navigateur** la voit (`browser_api_url`)

`api_public_url` répond à « comment un conteneur joint l'API ». Le navigateur de
l'administrateur et du visiteur vit sur l'hôte : en local, il joint l'API par
`http://localhost:8000`. Confondre les deux vues produit deux pannes
**silencieuses** :

| Consommateur | Valeur utilisée | Symptôme si `host.docker.internal` est utilisée |
| --- | --- | --- |
| Micro-tracker gravé dans le HTML (`BROWSER_API_URL`) | `browser_api_url` | `sendBeacon` échoue en résolution DNS : **aucun POST** visible dans l'onglet réseau, aucune audience enregistrée |
| `frame-ancestors` du serveur des vitrines (`PORTAL_ORIGIN`) | origine de `browser_api_url` | l'aperçu du Studio affiche « Ce site ne peut pas être affiché » / `frame-ancestors` dans la console |

Réglage : `browser_api_url` (groupe « Réseau & exposition », cockpit › Paramètres).
Vide → repli sur `api_public_url` (architecture recommandée : une seule URL
publique pour les deux vues). Les alias de bouclage `localhost` / `127.0.0.1`
sont ajoutés automatiquement au `frame-ancestors` : ce sont deux origines
distinctes pour un navigateur.

⚠️ `PORTAL_ORIGIN` et `BROWSER_API_URL` sont **figés à la création** du conteneur
(le HTML des vitrines est hydraté puis re-hydraté à chaque démarrage) : après un
changement de réglage, **recréez** les vitrines concernées
(`DELETE /admin/api/sites/<id>` puis redéploiement, ou l'action de reconstruction).

**Vérification** :
```bash
curl -sI http://localhost:<port_vitrine>/ | grep -i frame-ancestors
# frame-ancestors http://localhost:8000 http://127.0.0.1:8000   (et non host.docker.internal)
curl -s http://localhost:<port_vitrine>/ | grep -o 'analytics/hit/[^"]*'
# endpoint absolu joignable depuis le navigateur
```

**Vérification** :
```bash
docker exec <conteneur> python3 -c "import urllib.request;print(urllib.request.urlopen('http://<api>/internal/sync/content',timeout=5).status)"
# 401 attendu (jeton manquant) => l'API est joignable
```

### 1.3 🔴 `/` et `/editor` exigent une session

La page d'édition déclenche des actions d'infrastructure (publication, redémarrage)
et lisait un jeton dans le navigateur. Elle était servie sans authentification.

* Navigation non authentifiée → **302 vers `/login`**.
* Appel API non authentifié → **401**.

**À vérifier côté front** : tout lien ou `window.open('/editor')` doit être fait
après connexion ; sinon l'utilisateur est renvoyé au login (comportement voulu).

### 1.4 🟠 Le moteur de synchronisation dépend entièrement de l'API

Les conteneurs n'ont plus aucun accès direct à Supabase : ils appellent
`GET|POST /internal/sync/content` avec un jeton scopé. Conséquences :

* **Si l'API est indisponible au démarrage d'un conteneur**, ce dernier redémarre
  en boucle (choix assumé : ne jamais servir une vitrine sans le contenu du client).
  Le moteur retente 5 fois avec repli exponentiel (3 s → 30 s, soit jusqu'à ~2 min
  avec les délais réseau) avant d'échouer.
* **La rotation de `WAAS_SYNC_SECRET` (ou `WAAS_SECRET_KEY`)** invalide tous les
  jetons : il faut **recréer les conteneurs** aussitôt.
* Un conteneur arrêté plus d'une minute pendant une maintenance de l'API sera en
  échec au redémarrage : prévoyez une fenêtre ou démarrez l'API d'abord.

### 1.5 🟠 Le jeton n'est plus stocké dans le navigateur

`localStorage['waas_bearer_token']` est supprimé au chargement et n'est plus écrit.
La session repose sur le **cookie HttpOnly** ; le champ « jeton » de l'écran de
configuration reste utilisable mais **pour la session en cours uniquement**
(rechargement de page = ressaisie si vous n'utilisez pas le cookie).

### 1.6 🟠 Nouveau contrat de runtime pour les conteneurs

| Élément | Nouveau comportement |
|---|---|
| Rootfs | **lecture seule** ; seuls `/tmp`, `/run` (tmpfs) et `/var/www/html` (volume) sont inscriptibles |
| Utilisateur | `10001` — un template qui exige root ne fonctionnera plus |
| Contenu | re-cloné à **chaque démarrage** depuis `origin/main` (ou `TEMPLATE_REF`) puis copié **sans `.git`** |
| Persistance | tout ce qui est écrit hors volume est perdu au redémarrage |
| Ressources | 256 Mo, 0,5 vCPU, 128 PID : un template lourd peut être tué (OOM) — ajuster `mem_limit` au besoin |
| Logs | rotation 10 Mo × 3 ; les logs d'accès nginx vont sur stdout (donc visibles dans la console admin) |
| Réseau | réseau `waas-tenants`, communication inter-conteneurs **désactivée** |
| Santé | `HEALTHCHECK` : `docker ps` affiche `(healthy)` / `(unhealthy)` |

**Migration des conteneurs existants** : ils restent fonctionnels (l'API et la
télémétrie détectent `80/tcp` **et** `8080/tcp`), mais ils conservent l'ancien
environnement (donc la clé). Ils doivent être recréés (§3, étape 4).

### 1.7 🟠 Dépôts Git restreints

`REPO_URL` doit être **https**, sur un hôte de `allowed_git_hosts`
(`github.com,gitlab.com` par défaut), **sans identifiants** dans l'URL.
Un dépôt privé avec jeton (`https://token@github.com/...`) est désormais refusé :
le jeton finissait dans `.git/config`, lui-même servi publiquement.

**Dépôt privé (GitHub)** : définissez `WAAS_GITHUB_TOKEN` sur l'API (jeton
*fine-grained*, **Contents : Read-only**, limité aux dépôts clients, avec
expiration). Le conteneur ne reçoit jamais ce jeton : il démarre en
`SOURCE_MODE=api` et demande l'archive à `GET /internal/sync/source` avec son
`SYNC_TOKEN` **et** sa clé `SOURCE_NONCE` (aléatoire, propre à ce conteneur) ;
l'API la télécharge sur GitHub puis la lui remet. Garanties :

* dépôt lu dans l'environnement provisionné du conteneur, jamais dans la
  requête ; la clé du conteneur **courant** est exigée en plus du jeton HMAC
  (déterministe et permanent) : un jeton forgé avec un secret par défaut, ou
  hérité d'un conteneur détruit dont l'identifiant a été réattribué, ne suffit pas ;
* redirections suivies uniquement vers `api.github.com` / `codeload.github.com`
  (le jeton n'est présenté qu'à l'API, jamais au CDN ; l'URL signée du CDN est
  masquée dans les journaux `httpx`) ;
* archive téléchargée **entièrement** avant d'être remise (≤ 64 Mo, ≤ 120 s),
  avec une longueur exacte : jamais de site partiel ; le conteneur vérifie en plus
  la fin et le CRC du flux gzip ;
* 20 téléchargements par locataire / 10 min (échecs compris), un seul à la fois
  par locataire, 4 au total : un conteneur en boucle ne peut ni épuiser le quota
  GitHub partagé, ni bloquer le démarrage des autres. Ces limites sont **par
  processus** API (`--workers 2` les double) ;
* extraction ≤ 96 Mo avec le filtre `data` de `tarfile` (aucun fichier ni lien hors
  du site, aucun fichier spécial), refusée sur un Python non corrigé
  (CVE-2025-4517 : 3.11.13 minimum pour l'image).

Jeton expiré ou révoqué : les dépôts **publics** restent servis (repli anonyme),
seuls les privés échouent, avec une erreur explicite à la saisie.
Rotation : régénérer le jeton sur GitHub, mettre à jour `WAAS_GITHUB_TOKEN`,
redémarrer l'API ; les conteneurs n'ont pas à être recréés. Une fuite du jeton ne
donne qu'un accès en lecture aux dépôts autorisés : révoquez-le sur GitHub.

> ⚠️ **Tout le dépôt est publié**, privé ou non : nginx ne bloque que les fichiers
> cachés et quelques extensions (`.env`, `.sql`, `.yml`…). Un dépôt privé ne doit
> contenir que les fichiers du site (pas de données, sources ni documents internes).

### 1.8 🟡 Contrôles d'entrée désormais stricts

* **Connexion** : 5 échecs pour un couple (IP, identifiant) ⇒ blocage 15 min
  (`429` + `Retry-After`). Un administrateur qui se trompe 5 fois doit attendre.
* **Sondes** : méthodes limitées à `GET`/`HEAD`, ports sensibles interdits,
  `x-forwarded-for` n'est cru que derrière un proxy local.
* **Uploads** : 5 Mo, formats image uniquement.
* **En-têtes portail** : CSP (inline autorisé), `nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy`, HSTS dès que la requête arrive en HTTPS.
  ⚠️ La CSP interdit l'encadrement du portail : un outil interne qui l'affichait
  dans une iframe ne fonctionnera plus. Tout nouveau script **externe** doit être
  ajouté explicitement à la CSP (`main.py`, constante `PORTAL_CSP`).

### 1.9 🟡 Suppression d'un site = suppression du volume

`destroy_site` supprime désormais le conteneur **et** son volume (`v=True`).
L'ancien comportement laissait des volumes orphelins (fuite disque).

---

## 2. Ce qui n'a pas été fait volontairement

### 2.1 🔴 La clé `service_role` reste en repli dans `db.py`

**Pourquoi** : la supprimer fait échouer l'API au démarrage (et elle tourne en
`--reload`, donc à la moindre modification de fichier) tant que la variable
d'environnement n'est pas définie. Un correctif qui coupe la production n'est pas
un correctif.

**Ce qui a été fait à la place** : avertissement au démarrage **et audit qui
signale à chaque boot** les conteneurs porteurs d'un secret du plan de contrôle
(vérifié en exécution : `FUITES DE SECRETS : 2 variable(s) …`).

**À faire, dans cet ordre exact** :

```bash
# 1) Nouvelle clé : Supabase > Settings > API > Service role key > "Reset"
# 2) Export sur le serveur (jamais dans le dépôt)
export SUPABASE_SERVICE_KEY='<nouvelle clé>'
export SUPABASE_URL='https://<ref>.supabase.co'
export WAAS_SECRET_KEY="$(openssl rand -hex 32)"
export WAAS_SYNC_SECRET="$(openssl rand -hex 32)"
export WAAS_ANALYTICS_PEPPER="$(openssl rand -hex 32)"
# 3) Redémarrer l'API et vérifier l'absence de l'avertissement
#    "[DB Warn] Clé service_role par défaut utilisée"
# 4) Alors seulement, supprimer le repli dans End/db.py :
#    - _DEFAULT_SUPABASE_URL / _DEFAULT_SUPABASE_KEY (les deux constantes)
#    - la ligne if SUPABASE_KEY == _DEFAULT_SUPABASE_KEY: ...
#    et durcir :
```

```python
# End/db.py — après rotation
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError(
        "SUPABASE_URL et SUPABASE_SERVICE_KEY sont obligatoires : "
        "refus de démarrer avec des identifiants par défaut."
    )
```

5. Si un dépôt distant (GitHub/GitLab) contient des commits, purger l'historique :
   ```bash
   git filter-repo --path End/db.py --invert-paths --force   # puis réécrire l'historique distant
   ```
   (Le checkout local n'a aucun commit : rien à purger ici.)

### 2.2 🟠 Le RLS Supabase n'est pas activé — et ne suffit pas seul

**Point clé** : activer le RLS **ne protège pas** de la clé utilisée par l'API,
puisque `service_role` le contourne par conception. La vraie parade est double :

**a) Remplacer `service_role` par un rôle dédié à privilèges minimaux**

```sql
-- Supabase > SQL Editor
create role waas_api login password '<mot de passe long et aléatoire>';
grant usage on schema public to waas_api;

grant select, insert, update, delete on public.tenant_content   to waas_api;
grant select, insert, update, delete on public.tenant_monitors  to waas_api;
grant select, insert                 on public.tenant_analytics to waas_api;
grant select, insert, update, delete on public.fleet_settings   to waas_api;
grant select, insert, update         on public.waas_users       to waas_api;

grant execute on function public.verify_user_credentials(text, text) to waas_api;
grant execute on function public.get_tenant_analytics_summary(text)  to waas_api;

grant usage on schema storage to waas_api;
grant select, insert, update, delete on storage.objects to waas_api;  -- bucket waas-media
```

Générez ensuite un JWT signé avec le **JWT secret du projet** (Settings > API)
portant `"role": "waas_api"`, et utilisez-le comme `SUPABASE_SERVICE_KEY` :

```python
# tools/mint_api_jwt.py — à exécuter hors du dépôt, jeton non commité
import time, jwt
secret = "<JWT secret du projet>"
print(jwt.encode({"role": "waas_api", "iss": "supabase", "ref": "<project-ref>",
                  "iat": int(time.time()), "exp": int(time.time()) + 10*365*86400},
                 secret, algorithm="HS256"))
```

**b) Activer le RLS en refus par défaut** (protège des clés `anon`/`authenticated`
et de toute clé non applicative) :

```sql
alter table public.tenant_content   enable row level security;
alter table public.tenant_analytics enable row level security;
alter table public.tenant_monitors  enable row level security;
alter table public.fleet_settings   enable row level security;
alter table public.waas_users       enable row level security;
-- Aucune policy n'est créée : anon et authenticated ne voient rien.
-- Le plan de contrôle garde l'accès complet :
alter role waas_api bypassrls;      -- nécessite le rôle postgres (Supabase : oui)
-- Si bypassrls est refusé, ajouter à la place, pour chaque table :
-- create policy waas_api_all on public.tenant_content
--   for all to waas_api using (true) with check (true);
```

> ⚠️ À tester d'abord sur un projet Supabase de recette : une policy mal écrite
> peut couper l'API ou exposer des données.

**c) Table `sites` manquante** : `select * from public.sites` renvoie
`PGRST205: Could not find the table 'public.sites'`. Toute la logique de propriété
repose donc sur `waas_users.tenant_id`. Créez la table (avec `client_id`, `id`,
`name`, `domain`, `created_at`) ou assumez explicitement le repli — dans les deux
cas, `verify_site_ownership` refuse désormais par défaut (fail-closed).

### 2.3 🟠 L'API garde un accès root-équivalent au démon Docker

`docker.from_env()` donne le droit de monter l'hôte, de lancer un conteneur
privilégié, de lire tous les secrets. Une RCE dans l'API compromet la machine.

**Étape pragmatique — proxy de socket** (`tecnativa/docker-socket-proxy`) :

```yaml
# docker-compose.socket-proxy.yml — à exécuter sur l'hôte
services:
  dockerproxy:
    image: tecnativa/docker-socket-proxy:0.3
    environment:
      CONTAINERS: 1     # lecture des conteneurs
      IMAGES: 1         # lecture des images
      NETWORKS: 1       # création du réseau des locataires
      POST: 1           # start/stop/restart/create/remove
      EXEC: 0           # jamais
      VOLUMES: 0
      INFO: 0
      SWARM: 0
      SYSTEM: 0
      AUTH: 0
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    ports:
      - "127.0.0.1:2375:2375"
```
Puis démarrez l'API avec `DOCKER_HOST=tcp://127.0.0.1:2375`.
**Conséquence de `EXEC: 0`** : la console des fiches de site et la **publication à
chaud** du Studio (`sync_engine.py --rehydrate`, exécuté sous l'UID 10001 du
conteneur, sans réseau supplémentaire ni secret) deviennent indisponibles. La
publication retombe alors automatiquement sur le redémarrage du conteneur
(reclonage, coupure de quelques secondes) : rien ne casse, la mise en ligne est
seulement plus lente. Autoriser `EXEC: 1` rétablit les deux fonctions.
**Limite** : le proxy filtre par *type d'appel*, pas par étiquette — il n'empêche
pas l'API de supprimer un conteneur hors WaaS. Pour cela, `opa-docker-authz` avec
une règle restreinte au label `service=waas-tenant`.

### 2.4 🟠 `uvicorn --reload` et la configuration de lancement

`--reload` est un serveur de développement (surveillance de fichiers, sous-processus).
En production :

```bash
uvicorn main:app --host 172.17.0.1 --port 8000 --workers 2 --proxy-headers \
        --forwarded-allow-ips 127.0.0.1 --no-server-header
```
`--forwarded-allow-ips` est important : le rate-limiting et l'IP cliente
n'acceptent `X-Forwarded-For` que depuis un proxy local.

### 2.5 🟡 Secrets par défaut encore acceptés

`WAAS_SECRET_KEY`, `WAAS_ANALYTICS_PEPPER`, `WAAS_SYNC_SECRET` ont des valeurs par
défaut publiques. Tant qu'elles sont utilisées, **un JWT administrateur est
forgeable**. Le code journalise un avertissement : cherchez-le dans vos logs.
La rotation de `WAAS_SECRET_KEY` déconnecte tous les utilisateurs (comportement
attendu).

### 2.6 🟡 Ce qui n'est pas couvert par le code

| Sujet | État | À faire |
|---|---|---|
| Terminaison TLS | hors dépôt | Let's Encrypt/Caddy sur le reverse-proxy, HSTS |
| WAF / anti-DDoS / limitation globale | absent | Cloudflare ou `limit_req` sur le portail ; le limiteur de connexion est en mémoire (par processus) |
| Journal d'audit des actions admin | absent | table `admin_audit(who, action, target, ip, at)` + écriture dans `deploy_tenant`, `destroy_site`, `admin_manage_site_action`, `update_fleet_setting` |
| Alerting | absent | alerter sur `FUITES DE SECRETS`, `status=CRITICAL`, `unhealthy`, 429 en masse |
| Uploads : type réel du fichier | déclaratif | vérifier les octets magiques (`imghdr`/`Pillow`) et refuser SVG, ou servir les médias depuis un domaine séparé sans cookie |
| Sauvegardes | inconnu | sauvegarde chiffrée + **test de restauration** trimestriel |
| Isolation noyau | Docker partagé | `cap_drop`, seccomp par défaut, pas d'`--privileged` ; `userns-remap` possible mais casse les volumes/permissions |
| Rotation des secrets | manuelle | calendrier (90 j) + procédure testée (elle coupe les jetons de synchronisation) |

---

## 3. Feuille de route « complètement sécurisé »

### Étape 0 — Sauvegardes (30 min)
```bash
# Dump logique Supabase (ou backup PITR du projet) + inventaire des volumes
docker volume ls | grep waas
```
**Sortie** : dump restauré avec succès sur un projet de recette.

### Étape 1 — Secrets (1 h)
1. Régénérer la clé `service_role` dans Supabase.
2. Exporter `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `WAAS_SECRET_KEY`,
   `WAAS_SYNC_SECRET`, `WAAS_ANALYTICS_PEPPER` (voir §2.1).
3. Redémarrer l'API, vérifier qu'aucun avertissement de secret par défaut n'apparaît.
**Sortie** : logs sans « valeur par défaut » ; l'ancienne clé renvoie `401`.

### Étape 2 — API (30 min)
1. Lier l'API (`--host 172.17.0.1` ou HTTPS public), retirer `--reload` (§2.4).
2. `api_public_url` = URL joignable depuis les conteneurs (§1.2) — sinon le
   déploiement renvoie 422.
3. Supprimer le repli codé en dur de `db.py` (§2.1).
**Sortie** : `curl` de l'API depuis un conteneur renvoie 401 (donc joignable).

### Étape 3 — Conteneurs (1 h)
```bash
docker build -t waas-base-image:latest waas-base-image
# puis, pour chaque site : DELETE /admin/api/sites/<site> (si recréation complète)
#   ou POST /admin/api/sites/deploy avec le même identifiant après suppression
```
**Sortie** (par conteneur) :
```bash
docker inspect <c> --format 'user={{.Config.User}} ro={{.HostConfig.ReadonlyRootfs}} cap={{json .HostConfig.CapDrop}} mem={{.HostConfig.Memory}} pids={{.HostConfig.PidsLimit}} health={{.State.Health.Status}}'
docker exec <c> env | grep -ci supabase          # 0 attendu
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<port>/.git/config   # 404
```
Et dans les logs API : plus aucune ligne `FUITES DE SECRETS`.

### Étape 4 — Exposition réseau (2 h)
Reverse-proxy + TLS par site, `client_base_domain` mis à jour, `tenant_port_binding`
laissé à `127.0.0.1`.
**Sortie** : `https://<site>.<domaine>` répond en HTTPS valide ; le port direct
n'est plus joignable depuis l'extérieur.

### Étape 5 — Base de données (3 h)
Rôle `waas_api` + RLS (§2.2), testés sur recette.
**Sortie** : `select rolname, rolbypassrls from pg_roles where rolname='waas_api';`
renvoie une ligne ; une requête PostgREST avec la clé `anon` sur `tenant_content`
renvoie `[]` (RLS actif).

### Étape 6 — Isolation Docker (2 h)
Proxy de socket (§2.3), `DOCKER_HOST` pointant dessus.
**Sortie** : `docker -H tcp://127.0.0.1:2375 exec ...` est refusé, les opérations WaaS
fonctionnent toujours.

### Étape 7 — Détection (2 h)
Audit des actions admin (§2.6) + alertes sur les journaux critiques.
**Sortie** : un déploiement de test apparaît dans la table d'audit ; une alerte
se déclenche sur un conteneur `unhealthy` provoqué volontairement.

### Étape 8 — Chaîne de build (3 h)
```dockerfile
# épingler par digest et supprimer la chaîne de compilation (déjà réduite à git+nginx)
FROM python:3.11.16-alpine3.22@sha256:<digest>
```
```bash
# CI minimale
trivy image --exit-code 1 --severity HIGH,CRITICAL waas-base-image:latest
uv pip audit || pip-audit -r requirements.lock
docker build --no-cache --pull ...
```
**Sortie** : build reproductible, scan bloquant, SBOM archivé.

### Étape 9 — Hygiène continue
Rotation trimestrielle des secrets (avec la procédure « recréer les conteneurs »),
revue des accès Supabase, test de restauration, mise à jour mensuelle des images.

---

## 4. Vérifications permanentes (à relancer après chaque déploiement)

```bash
# Suites de non-régression (207 vérifications)
.venv/Scripts/python.exe tests/test_telemetry_hardening.py
.venv/Scripts/python.exe tests/test_tenant_isolation.py
.venv/Scripts/python.exe tests/test_api_hardening.py

# Aucun secret dans les conteneurs clients
docker ps -q --filter label=service=waas-tenant | while read c; do
  n=$(docker exec "$c" env | grep -ciE 'supabase|secret|waas_secret' || true)
  echo "$(docker inspect -f '{{.Name}}' "$c") : secrets=$n"
done

# Dépôt Git non servi + en-têtes
curl -s -o /dev/null -w '/.git/config -> %{http_code}\n' http://127.0.0.1:<port>/.git/config

# Éditeur protégé
curl -s -o /dev/null -w '%{http_code}\n' -H 'Accept: application/json' http://<api>/editor   # 401
```

---

## 5. Risques résiduels assumés (à connaître)

1. **L'API détient une clé à large portée** : même avec le rôle `waas_api`
   restreint, une compromission de l'hôte API expose les données de tous les
   locataires (c'est le prix du modèle « plan de contrôle unique »).
2. **Pas d'isolation noyau** entre locataires : conteneurs Docker partagés, donc
   une faille noyau reste une évasion possible (atténuée par `cap_drop ALL`,
   rootfs lecture seule, non-root, seccomp par défaut).
3. **CSP avec `'unsafe-inline'`** : nécessaire à l'architecture actuelle de l'UI
   (gestionnaires `onclick`, `<style>` en ligne). La CSP ne remplace pas les
   corrections XSS déjà appliquées ; passer à des scripts externes permettrait de
   la durcir.
4. **Gabarits Git** : contenu fourni par un administrateur, non signé par défaut.
   `TEMPLATE_REF` permet d'épingler un commit ; activez la vérification de
   signature (`git verify-commit`) si vos gabarits sont signés.
5. **Limiteur de connexion en mémoire** : par processus. Avec plusieurs workers
   ou plusieurs instances, la limite effective est multipliée — un stockage
   partagé (Redis) serait requis pour une limite globale.
