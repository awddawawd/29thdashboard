FROM python:3.11-alpine

# Seules les dépendances d'exécution : lxml est installé depuis un wheel musllinux
# (aucune chaîne de compilation n'est conservée dans l'image finale).
RUN apk add --no-cache git nginx \
 && addgroup -g 10001 -S waas \
 && adduser -u 10001 -S -G waas -h /home/waas waas \
 && mkdir -p /opt/scripts /var/www/html /tmp/nginx-server.d /home/waas /var/lib/nginx/logs \
 && chown -R 10001:10001 /opt/scripts /var/www/html /home/waas /var/lib/nginx
RUN pip install --no-cache-dir lxml

# Configuration nginx maîtrisée : tout ce qui est écrit va vers stdout/stderr ou /tmp,
# ce qui permet d'exécuter le conteneur en lecture seule et sans privilèges.
RUN printf '%s\n' \
    'worker_processes auto;' \
    'error_log /dev/stderr warn;' \
    'pid /tmp/nginx.pid;' \
    'events { worker_connections 512; }' \
    'http {' \
    '    include /etc/nginx/mime.types;' \
    '    default_type application/octet-stream;' \
    '    access_log /dev/stdout;' \
    '    client_body_temp_path /tmp/nginx-client;' \
    '    proxy_temp_path /tmp/nginx-proxy;' \
    '    fastcgi_temp_path /tmp/nginx-fastcgi;' \
    '    uwsgi_temp_path /tmp/nginx-uwsgi;' \
    '    scgi_temp_path /tmp/nginx-scgi;' \
    '    server_tokens off;' \
    '    sendfile on;' \
    '    keepalive_timeout 15;' \
    '    client_max_body_size 8m;' \
    '    limit_req_zone $binary_remote_addr zone=waas_perip:10m rate=20r/s;' \
    '    include /tmp/nginx-server.d/*.conf;' \
    '}' > /etc/nginx/nginx.conf \
 && rm -f /etc/nginx/http.d/*.conf

WORKDIR /opt/scripts
COPY sync_engine.py /opt/scripts/sync_engine.py
# Modèle de contenu v2 : sa présence active la publication à chaud côté API.
COPY waas_content.py /opt/scripts/waas_content.py
COPY fetch_source.py /opt/scripts/fetch_source.py
# Pont d'édition du Studio : parties assemblées par sync_engine au démarrage.
COPY editor /opt/scripts/editor
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER 10001:10001
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=40s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/ || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["nginx", "-e", "/dev/stderr", "-g", "daemon off;"]
