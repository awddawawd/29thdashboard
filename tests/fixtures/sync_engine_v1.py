"""Moteur de synchronisation v1 (archivé, NE PAS DÉPLOYER).

Conservé tel quel pour les tests de non-régression : les clés qu'il a
enregistrées en base doivent rester valides pour le moteur v2
(tests/test_editor_content.py, section 2).
"""

"""Moteur de synchronisation du conteneur locataire.

Ce processus s'exécute **dans le conteneur du client**, c'est-à-dire dans un
environnement non fiable. Il ne détient donc aucun secret du plan de contrôle :

* plus de clé Supabase ``service_role`` (elle donnait accès aux données de toute
  la flotte depuis n'importe quel conteneur compromise) ;
* un simple jeton HMAC ``SYNC_TOKEN``, scopé à ``TENANT_ID``, transmis à l'API
  WaaS qui applique le filtrage locataire côté serveur ;
* uniquement la bibliothèque standard pour les appels HTTP (aucune dépendance
  supplémentaire dans l'image, surface d'attaque réduite).
"""

import json
import os
import re
import sys
import time
from typing import Any
import urllib.error
import urllib.request
from urllib.parse import quote

from lxml import etree

# ---------------------------------------------------------
# VARIABLES D'ENVIRONNEMENT RUNTIME
# ---------------------------------------------------------

TENANT_ID = os.getenv("TENANT_ID")
SYNC_TOKEN = os.getenv("SYNC_TOKEN")
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
#: URL de l'API **vue du navigateur du visiteur**, utilisée pour le seul
#: micro-tracker. ``API_BASE_URL`` répond à « comment le conteneur joint l'API »
#: (souvent ``http://host.docker.internal:<port>``) : cet hôte ne résout pas
#: depuis le poste du visiteur, donc ``sendBeacon`` échouait en DNS — en
#: silence, sans requête visible dans l'onglet réseau. Repli sur
#: ``API_BASE_URL`` pour les images antérieures à ce réglage.
BROWSER_API_URL = (os.getenv("BROWSER_API_URL") or "").strip() or API_BASE_URL
WEB_ROOT = os.getenv("WEB_ROOT", "/var/www/html")

SYNC_ENDPOINT = "/internal/sync/content"
SYNC_TIMEOUT = float(os.getenv("SYNC_TIMEOUT", "30"))
SYNC_ATTEMPTS = int(os.getenv("SYNC_ATTEMPTS", "5"))
SYNC_PUSH_CHUNK = 500
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
USER_AGENT = os.getenv("WAAS_USER_AGENT", "Mozilla/5.0 (compatible; WaaS-SyncEngine/1.0)")

if not TENANT_ID or not SYNC_TOKEN:
    print(
        "[SyncEngine - ERREUR] TENANT_ID ou SYNC_TOKEN manquant : ce conteneur n'a pas été "
        "provisionné par l'API WaaS. Recréez-le via /admin/api/sites/deploy."
    )
    sys.exit(1)


# ---------------------------------------------------------
# ACCÈS AU PLAN DE DONNÉES (API SCOPÉE PAR JETON)
# ---------------------------------------------------------

def _api_call(
    method: str,
    path: str,
    payload: Any | None = None,
    timeout: float = SYNC_TIMEOUT,
    attempts: int = SYNC_ATTEMPTS,
) -> dict[str, Any]:
    """Appel authentifié à l'API WaaS, avec repli exponentiel borné.

    Lève si l'API est injoignable : mieux vaut redémarrer le conteneur que servir
    un site non hydraté (le contenu personnalisé du client serait absent).
    """
    url = f"{API_BASE_URL.rstrip('/')}{path}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("X-WAAS-Sync-Token", SYNC_TOKEN)
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", USER_AGENT)
        if body is not None:
            request.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES)
            return json.loads(raw.decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(2048).decode("utf-8", "replace")
            except Exception:
                pass
            print(f"[SyncEngine - API] HTTP {exc.code} sur {path} : {detail}")
            if exc.code in (400, 401, 403, 413, 422):
                raise RuntimeError(f"Refus de l'API de synchronisation (HTTP {exc.code}).") from exc
            last_error = exc
        except Exception as exc:  # réseau, DNS, timeout, TLS…
            last_error = exc

        if attempt < attempts:
            delay = min(3 * 2 ** (attempt - 1), 30)
            print(
                f"[SyncEngine - API] {method} {path} tentative {attempt}/{attempts} échouée "
                f"({last_error}) — nouvel essai dans {delay}s"
            )
            time.sleep(delay)

    raise RuntimeError(f"API de synchronisation injoignable ({path}) : {last_error}")


def fetch_content() -> list[dict[str, Any]]:
    """Contenu déjà enregistré pour ce locataire (jeton = seul discriminant)."""
    response = _api_call("GET", SYNC_ENDPOINT)
    records = response.get("records") or []
    return [r for r in records if isinstance(r, dict)]


def push_content(records: list[dict[str, Any]]) -> int:
    """Envoie les enregistrements extraits, par lots bornés."""
    written = 0
    for start in range(0, len(records), SYNC_PUSH_CHUNK):
        chunk = records[start:start + SYNC_PUSH_CHUNK]
        response = _api_call("POST", SYNC_ENDPOINT, payload={"records": chunk})
        written += int(response.get("upserted") or 0)
    return written


# ---------------------------------------------------------
# MICRO-TRACKER SANS COOKIE & EDITOR BRIDGE
# ---------------------------------------------------------

# URL d'analytics sérialisée en JSON : une valeur de réglage malformée
# (`browser_api_url`) ne doit jamais pouvoir casser la chaîne JS ni injecter du
# script dans toutes les vitrines (XSS stockée de flotte). Elle doit être
# joignable **depuis le navigateur du visiteur** (voir BROWSER_API_URL).
_ANALYTICS_ENDPOINT = (
    f"{BROWSER_API_URL.rstrip('/')}/waas/analytics/hit/{quote(str(TENANT_ID), safe='')}"
)

TRACKER_SNIPPET = f"""
<!-- WAAS RUNTIME ANALYTICS (Zero-Cookie / Ephemeral Daily Hash) -->
<script>
(function() {{
  try {{
    var payload = JSON.stringify({{ path: window.location.pathname }});
    var endpoint = {json.dumps(_ANALYTICS_ENDPOINT)};
    if (navigator.sendBeacon) {{
      navigator.sendBeacon(endpoint, new Blob([payload], {{ type: 'application/json' }}));
    }} else {{
      fetch(endpoint, {{ method: 'POST', body: payload, keepalive: true, headers: {{ 'Content-Type': 'application/json' }} }});
    }}
  }} catch(e) {{}}
}})();
</script>
"""

EDITOR_BRIDGE_SNIPPET = """
<!-- WAAS IN-CONTEXT VISUAL EDITOR BRIDGE -->
<script>
(function() {
  if (new URLSearchParams(window.location.search).get('waas_edit') === 'true') {
    const style = document.createElement('style');
    style.innerHTML = `
      [data-waas-xpath] { transition: outline 0.15s ease; cursor: pointer; }
      [data-waas-xpath]:hover { outline: 2px dashed #3b82f6 !important; outline-offset: 2px; }
      [data-waas-xpath]:focus { outline: 2px solid #2563eb !important; background-color: rgba(59, 130, 246, 0.05); }
    `;
    document.head.appendChild(style);

    // 1. Double-clic pour éditer un texte inline
    document.addEventListener('dblclick', (e) => {
      const target = e.target.closest('[data-waas-xpath]');
      if (!target || target.tagName === 'IMG') return;

      e.preventDefault();
      target.contentEditable = "true";
      target.focus();

      const onBlur = () => {
        target.contentEditable = "false";
        target.removeEventListener('blur', onBlur);
        // Notifie le dashboard parent de la modification
        window.parent.postMessage({
          type: 'WAAS_CONTENT_CHANGED',
          xpath: target.getAttribute('data-waas-xpath'),
          newValue: target.innerText.trim()
        }, '*');
      };
      target.addEventListener('blur', onBlur);
    });

    // 2. Clic sur une image pour demander au dashboard de changer l'URL
    document.addEventListener('click', (e) => {
      const target = e.target.closest('img[data-waas-xpath]');
      if (!target) return;

      e.preventDefault();
      window.parent.postMessage({
        type: 'WAAS_IMAGE_CLICKED',
        xpath: target.getAttribute('data-waas-xpath'),
        currentSrc: target.src
      }, '*');
    });

    // 3. Écoute de prévisualisation en direct depuis le parent
    window.addEventListener('message', (e) => {
      if (!e.data || !e.data.type) return;
      if (e.data.type === 'WAAS_UPDATE_PREVIEW' && e.data.xpath) {
        try {
          const el = document.querySelector(`[data-waas-xpath="${CSS.escape(e.data.xpath)}"]`);
          if (el) {
            if (el.tagName === 'IMG') {
              el.src = e.data.newValue;
            } else {
              el.innerText = e.data.newValue;
            }
          }
        } catch (err) {}
      }
    });
  }
})();
</script>
"""


def inject_scripts(html_content: str) -> str:
    """Injecte le tracker analytics et le script d'édition avant la balise </body>."""
    combined = ""
    if "WAAS RUNTIME ANALYTICS" not in html_content:
        combined += TRACKER_SNIPPET.strip() + "\n"
    if "WAAS IN-CONTEXT VISUAL EDITOR BRIDGE" not in html_content:
        combined += EDITOR_BRIDGE_SNIPPET.strip() + "\n"

    if not combined:
        return html_content

    if re.search(r"</body>", html_content, flags=re.IGNORECASE):
        return re.sub(r"</body>(?![\s\S]*</body>)", combined + "</body>", html_content, flags=re.IGNORECASE)
    return html_content + "\n" + combined


def inject_tracker(html_content: str) -> str:
    """Rétro-compatibilité : injecte l'ensemble des scripts nécessaires."""
    return inject_scripts(html_content)


TEXT_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "span", "a", "button", "li"}


# ---------------------------------------------------------
# UTILITAIRES XPATH
# ---------------------------------------------------------

def get_element_xpath(element: etree._Element) -> str:
    """Génère le XPath absolu via le RootTree du nœud."""
    return element.getroottree().getpath(element)


def annotate_tree_with_xpath(tree_root: etree._Element) -> None:
    """Tague automatiquement les éléments modifiables avec data-waas-xpath."""
    for elem in tree_root.iter():
        if not isinstance(elem.tag, str):
            continue
        tag = elem.tag.lower()
        if (tag in TEXT_TAGS and elem.text and elem.text.strip()) or tag == "img":
            elem.attrib["data-waas-xpath"] = get_element_xpath(elem)


# ---------------------------------------------------------
# EXTRACTION INITIALE (HTML -> API)
# ---------------------------------------------------------

def extract_content_from_html(file_path: str, rel_path: str) -> list[dict[str, Any]]:
    """Parse le HTML brut pour identifier les textes pertinents et les balises <img>."""
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        raw_html = f.read()

    parser = etree.HTMLParser(remove_blank_text=True)
    tree_root = etree.fromstring(raw_html, parser=parser)
    if tree_root is None:
        return []

    records = []
    text_tags = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "span", "a", "button", "li"}

    for elem in tree_root.iter():
        tag = elem.tag.lower() if isinstance(elem.tag, str) else ""

        if tag in text_tags and elem.text and elem.text.strip():
            clean_text = elem.text.strip()
            if len(clean_text) > 1 and not clean_text.startswith("{"):
                xpath = get_element_xpath(elem)
                records.append({
                    "file_path": rel_path,
                    "xpath": xpath,
                    "content_type": "text",
                    "content_value": clean_text[:5000],
                })

        elif tag == "img" and "src" in elem.attrib:
            src = elem.attrib["src"].strip()
            if src and not src.startswith("data:"):
                xpath = get_element_xpath(elem)
                records.append({
                    "file_path": rel_path,
                    "xpath": xpath,
                    "content_type": "image",
                    "content_value": src[:5000],
                })

    return records


# ---------------------------------------------------------
# HYDRATATION (API -> HTML)
# ---------------------------------------------------------

def hydrate_html_file(file_path: str, updates: dict[str, str]) -> None:
    """Injecte les valeurs personnalisées, ancre data-waas-xpath et ajoute les scripts."""
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        raw_html = f.read()

    parser = etree.HTMLParser()
    tree_root = etree.fromstring(raw_html, parser=parser)

    if tree_root is not None:
        if updates:
            for xpath, new_value in updates.items():
                try:
                    matched_elements = tree_root.xpath(xpath)
                    for el in matched_elements:
                        if el.tag.lower() == "img":
                            el.attrib["src"] = new_value
                        else:
                            el.text = new_value
                except Exception as e:
                    print(f"[SyncEngine - XPath Warn] Erreur sur {xpath}: {e}")

        # Attribution automatique au build (data-waas-xpath)
        annotate_tree_with_xpath(tree_root)

        hydrated_html = etree.tostring(tree_root, encoding="utf-8", method="html").decode("utf-8")
    else:
        hydrated_html = raw_html

    final_html = inject_scripts(hydrated_html)

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(final_html)


def collect_html_files(root_dir: str) -> list[tuple[str, str]]:
    """Liste les fichiers HTML du site sous forme ``(chemin absolu, chemin relatif)``."""
    html_files: list[tuple[str, str]] = []
    for root, _, files in os.walk(root_dir):
        for name in files:
            if name.lower().endswith((".html", ".htm")):
                abs_path = os.path.join(root, name)
                rel_path = os.path.relpath(abs_path, root_dir).replace("\\", "/")
                html_files.append((abs_path, rel_path))
    return html_files


# ---------------------------------------------------------
# POINT D'ENTRÉE PRINCIPAL DU PROCESSUS
# ---------------------------------------------------------

def run_sync() -> None:
    print(f"[SyncEngine] Démarrage du cycle pour le tenant : {TENANT_ID}")
    print(f"[SyncEngine] Répertoire de travail : {WEB_ROOT}")
    print(f"[SyncEngine] API de synchronisation : {API_BASE_URL}{SYNC_ENDPOINT}")
    print(f"[SyncEngine] Micro-tracker injecté (appelé par le navigateur) : {_ANALYTICS_ENDPOINT}")

    html_files = collect_html_files(WEB_ROOT)
    records = fetch_content()

    if not records:
        print("[SyncEngine] Premier lancement détecté : extraction des balises et images...")
        all_records: list[dict[str, Any]] = []
        for abs_path, rel_path in html_files:
            all_records.extend(extract_content_from_html(abs_path, rel_path))

        if all_records:
            written = push_content(all_records)
            print(f"[SyncEngine] Succès : {written}/{len(all_records)} éléments indexés en base.")
        else:
            print("[SyncEngine] Aucun élément textuel ou image exploitable trouvé.")

        for abs_path, _ in html_files:
            hydrate_html_file(abs_path, {})
    else:
        print("[SyncEngine] Contenu existant détecté : hydratation depuis l'API...")
        updates_by_file: dict[str, dict[str, str]] = {}
        for record in records:
            file_path = record.get("file_path")
            xpath = record.get("xpath")
            if not file_path or not xpath:
                continue
            updates_by_file.setdefault(file_path, {})[xpath] = record.get("content_value")

        for abs_path, rel_path in html_files:
            hydrate_html_file(abs_path, updates_by_file.get(rel_path, {}))

        print(f"[SyncEngine] Hydratation terminée sur {len(html_files)} fichier(s) HTML.")

    print("[SyncEngine] Prêt pour le démarrage de Nginx.")


if __name__ == "__main__":
    run_sync()
