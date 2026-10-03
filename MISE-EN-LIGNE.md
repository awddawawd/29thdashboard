# Mise en ligne — runbook WaaS

À suivre **dans l'ordre** le jour où vous publiez sur le VPS. Cochez au fur et à
mesure ; chaque étape a un critère de vérification.

> Détails de fond (RLS, socket proxy, audit admin, risques résiduels) :
> voir [GUIDE-SECURISATION.md](GUIDE-SECURISATION.md).

---

## 0. Prérequis (à faire avant le jour J)

- [ ] VPS Linux avec Docker installé (`docker --version`), 2 vCPU / 4 Go minimum
- [ ] Nom de domaine + **enregistrements DNS** :
  - `api.exemple.com` → IP du VPS (portail + API)
  - `*.sites.exemple.com` → IP du VPS (une vitrine par sous-domaine)
- [ ] Accès à la console Supabase (rotation des clés, SQL)
- [ ] Sauvegarde fraîche de la base Supabase **testée** (restauration comprise)

```bash
# Sur le VPS
sudo apt update && sudo apt install -y docker.io nginx certbot python3-certbot-nginx ufw
sudo usermod -aG docker "$USER"      # puis se reconnecter
```

---

## 1. Secrets et clés (🔴 à faire en premier)

- [ ] **Générer les secrets du serveur** (ne jamais réutiliser ceux du poste local) :

```bash
sudo mkdir -p /etc/waas && sudo chmod 700 /etc/waas
{
  echo "WAAS_SECRET_KEY=$(openssl rand -hex 32)"
  echo "WAAS_SYNC_SECRET=$(openssl rand -hex 32)"
  echo "WAAS_ANALYTICS_PEPPER=$(openssl rand -hex 32)"
} | sudo tee /etc/waas/api.env >/dev/null
sudo chmod 600 /etc/waas/api.env && sudo chown root:root /etc/waas/api.env
```

- [ ] **Supabase** : Settings → API
  - [ ] créer / régénérer une **Secret Key** (`sb_secret_…`)
  - [ ] **désactiver les « Legacy API keys »** : c'est le JWT `service_role` de
        219 caractères qui a fuité (présent dans le `.env` de votre poste **et**
        dans le conteneur client en cours). Tant qu'il est actif, la fuite l'est aussi.
- [ ] Compléter `/etc/waas/api.env` :

```bash
sudo tee -a /etc/waas/api.env >/dev/null <<'EOF'
SUPABASE_URL=https://<ref>.supabase.co
SUPABASE_SECRET_KEY=sb_secret_<...>
API_PUBLIC_URL=https://api.exemple.com
WAAS_ENABLE_DOCS=0
DOCKER_HOST=unix:///var/run/docker.sock
EOF
sudo chmod 600 /etc/waas/api.env
```

- [ ] *Si des vitrines sont hébergées sur des dépôts GitHub **privés*** : jeton
      *fine-grained* en lecture seule (**Contents : Read-only**, dépôts clients,
      avec expiration), ajouté **uniquement** à l'API — jamais aux conteneurs :

```bash
echo "WAAS_GITHUB_TOKEN=github_pat_<...>" | sudo tee -a /etc/waas/api.env >/dev/null
```

- [ ] **Ne pas** copier le `.env` du poste de développement sur le serveur
      (il contient l'ancienne clé et des secrets différents).

**Vérification** : `sudo grep -c 'sb_secret_' /etc/waas/api.env` → `1` ;
aucune ligne `eyJhbGciOi`.

---

## 2. Déployer l'API (systemd)

- [ ] Copier le code : `/opt/waas` (jamais le `.env` ni `.venv` du poste)

```bash
sudo mkdir -p /opt/waas && sudo chown "$USER":"$USER" /opt/waas
rsync -av --exclude '.venv' --exclude '.env' --exclude '__pycache__' \
      ./ vps:/opt/waas/
cd /opt/waas && uv sync --frozen            # ou python -m venv + pip install -r
```

- [ ] Créer `/etc/systemd/system/waas-api.service` :

```ini
[Unit]
Description=WaaS API (plan de contrôle)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=exec
User=waas
Group=waas
SupplementaryGroups=docker
WorkingDirectory=/opt/waas/End
EnvironmentFile=/etc/waas/api.env
# --reload est un mode DÉVELOPPEMENT : interdit en production.
# L'API doit être joignable depuis les conteneurs clients (sinon crashloop des vitrines).
ExecStart=/opt/waas/.venv/bin/uvicorn main:app \
    --host 0.0.0.0 --port 8000 --workers 2 \
    --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=always
RestartSec=3
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectHome=yes

[Install]
WantedBy=multi-user.target
```

```bash
sudo useradd -r -s /usr/sbin/nologin waas
sudo chown -R waas:waas /opt/waas
sudo systemctl daemon-reload && sudo systemctl enable --now waas-api
sudo journalctl -u waas-api -n 40 --no-pager
```

**Critères** (dans les journaux) :
- [ ] `Application startup complete`
- [ ] `Documentation interactive exposée…` **absent** (⇒ `WAAS_ENABLE_DOCS=0` pris en compte)
- [ ] `Clé service_role par défaut` **absent** et `Secret par défaut détecté` **absent**
- [ ] `FUITES DE SECRETS` : présent tant que les anciens conteneurs tournent (normal)
- [ ] `curl -s http://127.0.0.1:8000/docs -o /dev/null -w '%{http_code}\n'` → `404`

---

## 3. Reverse proxy + TLS (indispensable : les vitrines ne sont plus exposées)

### 3.1 Portail / API

`/etc/nginx/sites-available/waas-api` :

```nginx
server {
    listen 80;
    server_name api.exemple.com;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl http2;
    server_name api.exemple.com;

    ssl_certificate     /etc/letsencrypt/live/api.exemple.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.exemple.com/privkey.pem;

    client_max_body_size 8m;
    server_tokens off;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options "nosniff" always;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;   # une seule IP : jamais de liste chaînée
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 120s;                          # flux SSE des journaux
        proxy_buffering off;                              # SSE
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/waas-api /etc/nginx/sites-enabled/
sudo certbot --nginx -d api.exemple.com
sudo nginx -t && sudo systemctl reload nginx
```

### 3.2 Une vitrine par sous-domaine

Certificat **wildcard** (DNS-01, obligatoire pour `*.sites.exemple.com`) :

```bash
sudo certbot certonly --manual --preferred-challenges dns \
  -d sites.exemple.com -d '*.sites.exemple.com'
# ou via un plugin DNS (cloudflare, ovh…) : --dns-cloudflare --dns-cloudflare-credentials /etc/letsencrypt/cf.ini
```

Squelette à générer **par site déployé** (le port est renvoyé par
`POST /admin/api/sites/deploy` → `host_port`).

Déclarez d'abord **une seule fois** la zone de limitation, dans le bloc `http {}`
(fichier `/etc/nginx/conf.d/limits.conf`) :

```nginx
limit_req_zone $binary_remote_addr zone=waas_site:10m rate=20r/s;
```

Puis, pour chaque site :

```bash
cat >/etc/nginx/sites-available/site-<ID> <<EOF
server {
    listen 443 ssl http2;
    server_name <ID>.sites.exemple.com;

    ssl_certificate     /etc/letsencrypt/live/sites.exemple.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/sites.exemple.com/privkey.pem;

    server_tokens off;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    location / {
        limit_req zone=waas_site burst=40 nodelay;
        proxy_pass http://127.0.0.1:<PORT>;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto https;
    }
}
EOF
sudo ln -s /etc/nginx/sites-available/site-<ID> /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

- [ ] `client_base_domain` = `sites.exemple.com` (réglage `fleet_settings`)
- [ ] `api_public_url` = `https://api.exemple.com` (ou via `API_PUBLIC_URL`)
- [ ] `browser_api_url` = `https://api.exemple.com` — **laisser vide** si l'API est
      publique : la valeur de repli est `api_public_url`. À renseigner uniquement
      quand les deux vues diffèrent (en local : `api_public_url` =
      `http://host.docker.internal:8000` pour les conteneurs, `browser_api_url` =
      `http://localhost:8000` pour le navigateur). Ce réglage alimente l'endpoint
      du micro-tracker gravé dans le HTML des vitrines et le `frame-ancestors` qui
      autorise l'aperçu du Studio ; un changement n'agit qu'après recréation des
      vitrines.
- [ ] `tenant_port_binding` = `127.0.0.1` (défaut — laisser tel quel)

**Critères** : `https://api.exemple.com/docs` → 404 · `https://<site>.sites.exemple.com`
affiche la vitrine en HTTPS valide · `docker port waas-<site>` → `127.0.0.1:<port>` ·
la vitrine reçoit des `POST /waas/analytics/hit/<site>` dans l'onglet réseau (et non
des échecs DNS silencieux), et l'aperçu du Studio s'affiche dans l'iframe.

---

## 4. Image et conteneurs clients

- [ ] Reconstruire l'image durcie :

```bash
cd /opt/waas && sudo docker build -t waas-base-image:latest waas-base-image
```

- [ ] Recréer **chaque** vitrine (l'ancienne image garde la clé `service_role`) :
      `DELETE /admin/api/sites/<id>` (supprime aussi le volume) puis
      `POST /admin/api/sites/deploy` avec le même `site_id` et le même dépôt,
      ou recréation via votre console admin.

**Critères par conteneur** :

```bash
C=waas-<id>
sudo docker inspect $C --format 'user={{.Config.User}} ro={{.HostConfig.ReadonlyRootfs}} cap={{json .HostConfig.CapDrop}} mem={{.HostConfig.Memory}} pids={{.HostConfig.PidsLimit}} health={{.State.Health.Status}} net={{.HostConfig.NetworkMode}}'
# attendu : user=10001:10001 ro=true cap=["ALL"] mem=268435456 pids=128 health=healthy net=waas-tenants

sudo docker exec $C env | grep -ciE 'supabase|waas_secret'      # 0
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<port>/.git/config   # 404
sudo docker port $C                                              # 127.0.0.1:<port>
```

- [ ] Et dans les journaux de l'API : **plus aucune ligne `FUITES DE SECRETS`**
      (c'est le signal que la migration est terminée).

---

## 5. Pare-feu

```bash
sudo ufw default deny incoming
sudo ufw allow 22/tcp
sudo ufw allow 80,443/tcp
sudo ufw enable
sudo ufw status verbose
```

- [ ] Vérification : `8000`, `8080` et `2375` **ne doivent pas** être joignables
      depuis Internet (`nmap -Pn <ip> -p 8000,8080,2375`).
- [ ] ⚠️ Piège connu : un port **publié par Docker** contourne `ufw`. Ici les
      vitrines sont liées à `127.0.0.1` (donc non publiées) et `8000` appartient à
      un processus de l'hôte (donc filtré normalement). Si vous publiez un jour un
      port de conteneur sur `0.0.0.0`, ajoutez la règle dans la chaîne `DOCKER-USER` :

```bash
sudo iptables -I DOCKER-USER -p tcp --dport 8080 -j DROP
```

---

## 6. Durcissements recommandés (fortement conseillés avant l'ouverture publique)

- [ ] **Proxy de socket Docker** (l'API a aujourd'hui un accès root-équivalent) :
      voir §2.3 de [GUIDE-SECURISATION.md](GUIDE-SECURISATION.md), puis
      `DOCKER_HOST=tcp://127.0.0.1:2375` dans `/etc/waas/api.env`.
- [ ] **Rôle Postgres à privilèges minimaux + RLS** : §2.2 du guide (SQL prêt à coller).
- [ ] **Journal d'audit des actions admin** (table + écriture dans `deploy_tenant`,
      `destroy_site`, `admin_manage_site_action`, `update_fleet_setting`).
- [ ] **Supervision** :
  - `curl -fsS https://api.exemple.com/admin/api/fleet/health` (avec jeton admin) → `status`
  - alerter sur : `status=CRITICAL`, conteneur `unhealthy`, `FUITES DE SECRETS`,
    volume anormal de `429` (tentatives de connexion)
- [ ] **Sauvegardes** : dump Supabase quotidien chiffré + rétention 30 j +
      **test de restauration trimestriel**.
- [ ] **CI** : build épinglé par digest, `trivy image`, `pip-audit`.

---

## 7. Contrôles finaux (à passer en revue avant d'annoncer l'ouverture)

```bash
# 1. Suites de non-régression (207 vérifications)
cd /opt/waas
.venv/bin/python tests/test_telemetry_hardening.py
.venv/bin/python tests/test_tenant_isolation.py
.venv/bin/python tests/test_api_hardening.py

# 2. Portail
curl -sI https://api.exemple.com/login | grep -iE 'strict-transport|content-security|x-frame|nosniff'
curl -s -o /dev/null -w '%{http_code}\n' -H 'Accept: application/json' https://api.exemple.com/editor   # 401
curl -s -o /dev/null -w '%{http_code}\n' https://api.exemple.com/docs                                  # 404

# 3. Vitrines
curl -s -o /dev/null -w '%{http_code}\n' https://<site>.sites.exemple.com/.git/config                   # 404
curl -sI https://<site>.sites.exemple.com | grep -i 'frame-ancestors'                                   # frame-ancestors https://api.exemple.com

# 4. Aucun secret dans les conteneurs
for C in $(sudo docker ps -q --filter label=service=waas-tenant); do
  echo "$(sudo docker inspect -f '{{.Name}}' $C) : $(sudo docker exec $C env | grep -ciE 'supabase|waas_secret') secret(s)"
done

# 5. Environnement de l'API (aucune valeur par défaut)
sudo systemctl show waas-api -p Environment | tr ' ' '\n' | grep -cE 'service_role|change-in-production'   # 0
```

- [ ] Connexion admin OK, jeton admin opérationnel, tablette de bord alimentée
- [ ] Déploiement d'une vitrine de test de bout en bout (clone → sync → affichage)
- [ ] Publication d'une modification de contenu depuis l'éditeur (redémarrage du conteneur)
- [ ] Un 401/403 sur une ressource d'un autre locataire (test d'isolation manuel)

---

## 8. Retour arrière (si la mise en ligne se passe mal)

1. **Proxy** : `sudo unlink /etc/nginx/sites-enabled/site-<id> && sudo nginx -s reload`
2. **Vitrine inaccessible** : vérifier `sudo docker logs waas-<id>` → si le moteur
   de synchronisation échoue, l'API n'est pas joignable depuis le conteneur
   (`API_PUBLIC_URL`) ; corriger puis `docker restart`.
3. **Dépannage d'exposition** : `tenant_port_binding=0.0.0.0` dans `fleet_settings`
   rétablit l'ancien accès direct (avertissement journalisé).
4. **API** : `sudo systemctl stop waas-api` et restaurer la version précédente du
   code + `systemctl start`.
5. **Base** : restaurer le dump Supabase (les conteneurs re-synchroniseront à leur
   prochain redémarrage).

---

## 9. Pièges à ne pas oublier

| Piège | Conséquence | Parade |
|---|---|---|
| API liée à `127.0.0.1` seulement | les vitrines redémarrent en boucle | `--host 0.0.0.0` (filtré par ufw) + `API_PUBLIC_URL` |
| `WAAS_ENABLE_DOCS` laissé à `1` | `/docs` public + CSP affaiblie (`unsafe-eval` + CDN tiers) | `WAAS_ENABLE_DOCS=0` |
| Anciennes clés « Legacy » Supabase actives | la clé fuitée reste utilisable | les désactiver (§1) |
| Rotation de `WAAS_SECRET_KEY` / `WAAS_SYNC_SECRET` | **tous** les jetons de synchronisation invalidés | recréer les conteneurs juste après |
| `.env` copié sur le serveur | secrets en clair et différents du système | `EnvironmentFile` root:root 600 |
| Ports de vitrines publiés sur `0.0.0.0` | contournement du proxy/TLS et de `ufw` | laisser `tenant_port_binding=127.0.0.1` |
| `--reload` en production | serveur de développement, redémarrages intempestifs | service systemd sans `--reload` |
| Conteneurs non recréés | la clé `service_role` reste dans leur environnement | vérifier l'absence de `FUITES DE SECRETS` |
| Modification de la CSP du portail | écran blanc (cas déjà rencontré sur `/docs`) | tester `/`, `/login`, `/admin/dashboard` et `/editor` après tout changement de CSP |
