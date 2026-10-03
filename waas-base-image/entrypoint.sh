#!/bin/sh
# Point d'entrée d'un conteneur locataire.
#
# Le conteneur s'exécute sans privilèges (UID 10001), avec un rootfs en lecture
# seule : tout ce qui doit être écrit va dans /tmp (tmpfs) ou dans le volume
# /var/www/html. Aucun secret du plan de contrôle n'est présent dans
# l'environnement : uniquement un jeton SYNC_TOKEN scopé à ce locataire.
set -eu

log() { printf '[Entrypoint] %s\n' "$*"; }

# --- 1. Variables obligatoires -------------------------------------------------
[ -n "${TENANT_ID:-}" ] || { log "ERREUR : TENANT_ID manquant."; exit 1; }
[ -n "${REPO_URL:-}" ] || { log "ERREUR : REPO_URL manquant."; exit 1; }
[ -n "${SYNC_TOKEN:-}" ] || {
    log "ERREUR : SYNC_TOKEN manquant : ce conteneur n'a pas été provisionné par l'API WaaS"
    log "         (recréez-le via /admin/api/sites/deploy)."
    exit 1
}

SITE_DIR="/var/www/html"
SRC_DIR="/tmp/waas-src"
export HOME="/tmp"
export GIT_TERMINAL_PROMPT=0

# --- 2. Défense en profondeur sur la source du contenu ------------------------
# SOURCE_MODE (fixé par l'API au déploiement) :
#   git : clone public depuis REPO_URL, sans aucun identifiant ;
#   api : archive relayée par l'API WaaS (dépôt GitHub privé). Le jeton GitHub
#         reste dans l'API : ce conteneur ne présente que son SYNC_TOKEN scopé.
SOURCE_MODE="${SOURCE_MODE:-git}"
ALLOWED_REPO_HOSTS="${ALLOWED_REPO_HOSTS:-github.com,gitlab.com}"
case "$SOURCE_MODE" in
    git|api) : ;;
    *) log "ERREUR : SOURCE_MODE inconnu : ${SOURCE_MODE} (attendu : git ou api)."; exit 1 ;;
esac
case "$REPO_URL" in
    https://*) : ;;
    *) log "ERREUR : REPO_URL doit être une URL https://."; exit 1 ;;
esac
case "$REPO_URL" in
    *"@"*) log "ERREUR : identifiants interdits dans REPO_URL."; exit 1 ;;
esac
REPO_HOST=$(printf '%s' "$REPO_URL" | sed -E 's#^https://([^/]+).*#\1#' | sed -E 's#:.*$##')
case ",${ALLOWED_REPO_HOSTS}," in
    *",${REPO_HOST},"*) : ;;
    *) log "ERREUR : hôte Git non autorisé : ${REPO_HOST} (autorisés : ${ALLOWED_REPO_HOSTS})."; exit 1 ;;
esac

# --- 3. Configuration nginx (exécution sans privilèges, port non privilégié) ---
PORTAL_ORIGIN="${PORTAL_ORIGIN:-'self'}"
mkdir -p /tmp/nginx-server.d /tmp/nginx-client /tmp/nginx-proxy \
         /tmp/nginx-fastcgi /tmp/nginx-uwsgi /tmp/nginx-scgi
cat > /tmp/nginx-server.d/default.conf <<EOF
server {
    listen 8080;
    server_name _;
    root ${SITE_DIR};
    index index.html;

    # Anti-force brute : borne le débit par IP cliente.
    limit_req zone=waas_perip burst=40 nodelay;

    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    # Seule origine autorisée à encadrer la vitrine : le portail (aperçu de l'éditeur).
    # Aucun CORS ouvert : il ne servait qu'à lire les fichiers depuis un navigateur tiers.
    add_header Content-Security-Policy "frame-ancestors ${PORTAL_ORIGIN}" always;

    # Le dépôt Git n'est jamais servi, ni aucun fichier de configuration caché
    # (les défis ACME restent accessibles).
    location ^~ /.well-known/ { try_files \$uri \$uri/ =404; }
    location ~ /\. { deny all; return 404; }
    location ~* \.(git|env|bak|sql|log|ini|yml|yaml|toml|lock|sh|py)$ { deny all; return 404; }

    location / { try_files \$uri \$uri/ /index.html; }
}
EOF

# --- 4. Récupération du contenu (jamais de .git dans la racine web) -----------
rm -rf "$SRC_DIR"
if [ "$SOURCE_MODE" = "api" ]; then
    log "Récupération de ${REPO_URL} via l'API WaaS (aucun identifiant Git dans ce conteneur)..."
    python3 /opt/scripts/fetch_source.py "$SRC_DIR"
else
    log "Clonage du template depuis ${REPO_URL}..."
    # --no-tags : les tags ne sont pas récupérés ; protocoles dangereux désactivés.
    git -c protocol.ext.allow=never -c protocol.file.allow=never \
        clone --depth 1 --single-branch --no-tags "$REPO_URL" "$SRC_DIR"

    if [ -n "${TEMPLATE_REF:-}" ]; then
        log "Épinglage sur ${TEMPLATE_REF}..."
        git -C "$SRC_DIR" fetch --depth 1 --no-tags origin "$TEMPLATE_REF"
        git -C "$SRC_DIR" checkout --detach FETCH_HEAD
    fi
    log "Commit déployé : $(git -C "$SRC_DIR" rev-parse HEAD)"
fi

find "$SITE_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true
cp -a "$SRC_DIR"/. "$SITE_DIR"/
rm -rf "$SITE_DIR/.git"          # jamais de dépôt servi publiquement
rm -rf "$SRC_DIR"

# --- 5. Synchronisation du contenu via l'API (jeton scopé) --------------------
# Un échec fait échouer le démarrage : mieux vaut un redémarrage automatique
# qu'une vitrine servie sans le contenu personnalisé du client.
log "Lancement du moteur de synchronisation..."
python3 /opt/scripts/sync_engine.py

# --- 6. Serveur web -----------------------------------------------------------
log "Démarrage de Nginx (port 8080, utilisateur non privilégié)..."
exec "$@"
