"""Modèle de contenu éditable d'une vitrine — moteur v2.

Fonctions pures, sans réseau ni variable d'environnement : elles sont utilisées
par le moteur de synchronisation du conteneur (``sync_engine.py``) et testées
isolément (``tests/test_editor_content.py``).

Vocabulaire
-----------
* **emplacement** : une valeur éditable de la page, de trois natures :

  - ``text``  : un nœud texte visible (titre, bouton, suite d'un paragraphe
    après un ``<strong>`` ou un ``<br>``…) ;
  - ``image`` : la source d'une balise ``<img>`` (``src`` ou ``data-src``) ;
  - ``attr``  : un attribut lu par l'internaute (texte alternatif, texte
    d'aide d'un champ, description de la page pour les moteurs de recherche).

* **clé** : identité persistante de l'emplacement, sous forme d'XPath évaluable,
  toujours calculée sur le gabarit **d'origine** (jamais sur une page hydratée) :

  - ``/html/body/h1`` : texte de tête de l'élément (forme historique, v1) ;
  - ``/html/body/p/text()[normalize-space()][2]`` : 2ᵉ nœud texte non vide ;
  - ``/html/body/img`` : source d'une image (forme historique) ;
  - ``/html/body/img/@alt`` : attribut ;
  - ``//*[@data-waas-id='hero']/h1`` : élément ancré sur un identifiant stable
    posé par l'auteur du gabarit (résiste aux réorganisations de la page).

* **position** (``t0``, ``t3``, ``src``, ``@alt``) : emplacement de la valeur
  dans son élément porteur. ``t0`` est le texte de tête, ``tN`` le texte qui
  suit le N-ième nœud enfant non textuel (élément ou commentaire). Le pont
  d'édition retrouve ainsi le nœud dans le DOM sans recalculer de chemin.

La page publique reçoit un attribut ``data-waas="N"`` sur chaque élément
porteur ; la carte ``/__waas/map/<page>.json`` (chargée par le seul pont
d'édition) associe ces numéros aux clés.
"""

from __future__ import annotations

import codecs
import hashlib
import html as html_lib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional
from urllib.parse import quote

from lxml import etree

ENGINE_VERSION = 2
MAX_VALUE_LENGTH = 5000
MAX_URL_LENGTH = 2048
MAX_DEPTH = 400

#: Blancs au sens d'XPath (``normalize-space``) : l'espace insécable n'en fait pas partie.
XPATH_BLANKS = " \t\r\n"
_BLANK_RUN = re.compile(r"[ \t\r\n]+")
_LEADING_BLANKS = re.compile(r"^[ \t\r\n]*")
_TRAILING_BLANKS = re.compile(r"[ \t\r\n]*$")
#: Caractères refusés par XML 1.0, donc par lxml (ValueError à l'écriture) : contrôles
#: hors tabulation et sauts de ligne, demi-codets isolés, non-caractères U+FFFE / U+FFFF.
_CONTROL_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff￾￿]")
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_STABLE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:\-]+)""", re.IGNORECASE)
_DOCUMENT_TAG = re.compile(r"<(?:html|head|body)\b|<!doctype", re.IGNORECASE)
_BODY_CLOSE = re.compile(r"</body>(?![\s\S]*</body>)", re.IGNORECASE)
#: Gabarits côté navigateur (Vue, Alpine, Handlebars…) : les modifier casserait le rendu.
_TEMPLATE_MARKERS = ("{{", "}}", "{%", "%}", "${", "<%", "%>")

#: Sous-arbres jamais éditables : code, médias embarqués, contrôles de formulaire.
SKIP_SUBTREES = frozenset({
    "script", "style", "noscript", "template", "svg", "math", "iframe", "object",
    "canvas", "select", "textarea", "video", "audio",
})
INPUT_VALUE_TYPES = frozenset({"submit", "button", "reset"})

ANNOTATION_ATTR = "data-waas"
STABLE_ID_ATTR = "data-waas-id"
SKIP_ATTR = "data-waas-skip"

TRACKER_MARKER = "WAAS RUNTIME ANALYTICS"
LOADER_MARKER = "WAAS EDITOR LOADER"
ASSETS_PREFIX = "/__waas"


# ---------------------------------------------------------------------------
# Valeurs
# ---------------------------------------------------------------------------

def norm(value: Optional[str]) -> str:
    """Texte tel qu'affiché : blancs successifs réduits, bords rognés (insécables conservés)."""
    return _BLANK_RUN.sub(" ", value or "").strip(XPATH_BLANKS)


def clean_text(value: Any) -> Optional[str]:
    """Valeur texte sûre : caractères de contrôle retirés, longueur bornée, sinon ``None``."""
    if not isinstance(value, str):
        return None
    cleaned = _CONTROL_CHARS.sub("", value)
    return cleaned if len(cleaned) <= MAX_VALUE_LENGTH else None


def safe_image_url(value: Any) -> Optional[str]:
    """URL d'image publiable : http(s), chemin relatif ou absolu ; tout autre schéma refusé."""
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or len(url) > MAX_URL_LENGTH or _CONTROL_CHARS.search(url):
        return None
    if any(char in url for char in '<>"`\\') or any(char.isspace() for char in url):
        return None
    if _URL_SCHEME.match(url) and not url.lower().startswith(("http://", "https://")):
        return None
    return url


def slot_matches(slot: "Slot", value: str) -> bool:
    """La valeur est celle du gabarit (aux blancs près, sauf texte préformaté) : rien à injecter."""
    if slot.kind == "image":
        return value.strip() == slot.source.strip()
    if slot.preformatted:
        return value == slot.source
    return norm(value) == norm(slot.source)


def is_editable_text(value: str) -> bool:
    """Texte proposé à l'édition : au moins une lettre ou un chiffre, aucun gabarit JS."""
    if not value or value.startswith("{"):
        return False
    if any(marker in value for marker in _TEMPLATE_MARKERS):
        return False
    return any(char.isalnum() for char in value)


# ---------------------------------------------------------------------------
# Lecture / écriture des fichiers (encodage d'origine conservé)
# ---------------------------------------------------------------------------

def decode_html(raw: bytes) -> tuple[str, str]:
    """Décode un fichier HTML ; renvoie ``(texte, encodage)`` pour le réécrire à l'identique."""
    if raw.startswith(codecs.BOM_UTF8):
        return raw[len(codecs.BOM_UTF8):].decode("utf-8", "replace"), "utf-8-sig"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    declared = _META_CHARSET.search(raw[:4096])
    encoding = "cp1252"
    if declared:
        try:
            encoding = codecs.lookup(declared.group(1).decode("ascii")).name
        except (LookupError, UnicodeDecodeError):
            encoding = "cp1252"
    return raw.decode(encoding, "replace"), encoding


def encode_html(text: str, encoding: str) -> bytes:
    """Encode dans l'encodage d'origine ; l'inencodable devient une référence numérique."""
    return text.encode(encoding, "xmlcharrefreplace")


def _parser() -> etree.HTMLParser:
    # default_doctype=False : aucun DOCTYPE inventé (il changerait le mode de rendu).
    return etree.HTMLParser(default_doctype=False, remove_comments=False, no_network=True)


def _tag(node: Any) -> Optional[str]:
    tag = getattr(node, "tag", None)
    return tag.lower() if isinstance(tag, str) else None


# ---------------------------------------------------------------------------
# Emplacements
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Slot:
    """Valeur éditable d'une page (voir le vocabulaire en tête de module)."""

    key: str
    kind: str
    source: str
    tag: str
    position: str
    preformatted: bool = False
    element: Any = field(default=None, compare=False, repr=False)

    def inventory_entry(self) -> dict[str, Any]:
        entry: dict[str, Any] = {"xpath": self.key, "content_type": self.kind, "source": self.source}
        if self.preformatted:
            entry["exact"] = True               # <pre> : les blancs comptent
        return entry


@dataclass
class ApplyReport:
    applied: int = 0
    unknown: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RenderedPage:
    html: bytes
    map_json: Optional[str]
    map_version: Optional[str]


class _Extractor:
    """Parcours en ordre de document : un emplacement par valeur éditable."""

    def __init__(self, root: Any) -> None:
        self.tree = root.getroottree()
        self.slots: list[Slot] = []
        counts = Counter(
            node.get(STABLE_ID_ATTR) for node in root.iter()
            if isinstance(node.tag, str) and _STABLE_ID.match(node.get(STABLE_ID_ATTR) or "")
        )
        self.unique_ids = {value for value, count in counts.items() if count == 1}

    # --- clés -------------------------------------------------------------
    def element_key(self, element: Any) -> Optional[str]:
        steps: list[Any] = []
        node = element
        while node is not None:
            if _tag(node) is None or ":" in node.tag:
                return None              # balise à préfixe (Word : <o:p>) : XPath invalide
            stable = node.get(STABLE_ID_ATTR)
            if stable in self.unique_ids:
                relative = "/".join(_step(item) for item in reversed(steps))
                anchor = f"//*[@{STABLE_ID_ATTR}='{stable}']"
                return f"{anchor}/{relative}" if relative else anchor
            steps.append(node)
            node = node.getparent()
        return self.tree.getpath(element)

    # --- parcours ---------------------------------------------------------
    def walk(self, element: Any, depth: int = 0, preformatted: bool = False) -> None:
        tag = _tag(element)
        if tag is None or element.get(SKIP_ATTR) is not None or depth > MAX_DEPTH:
            return
        key = self.element_key(element)
        if key is None:
            return
        self._attribute_slots(element, tag, key)
        if tag in SKIP_SUBTREES:
            return
        preformatted = preformatted or tag == "pre"

        nonblank = 0
        if element.text and element.text.strip(XPATH_BLANKS):
            nonblank += 1
            self._text_slot(element, tag, key, "t0", element.text, preformatted)
        for index, child in enumerate(element, start=1):
            self.walk(child, depth + 1, preformatted)
            tail = child.tail
            if tail and tail.strip(XPATH_BLANKS):
                nonblank += 1
                self._text_slot(element, tag, f"{key}/text()[normalize-space()][{nonblank}]",
                                f"t{index}", tail, preformatted)

    def _text_slot(self, element: Any, tag: str, key: str, position: str, text: str,
                   preformatted: bool) -> None:
        value = text if preformatted else norm(text)
        if is_editable_text(norm(value)) and len(value) <= MAX_VALUE_LENGTH:
            self.slots.append(Slot(key, "text", value, tag, position, preformatted, element))

    def _attribute_slots(self, element: Any, tag: str, key: str) -> None:
        if tag == "img":
            source = _image_source(element)
            if source is not None:
                self.slots.append(Slot(key, "image", source, tag, "src", element=element))
            self._attr_slot(element, tag, key, "alt")
        elif tag in ("input", "textarea"):
            self._attr_slot(element, tag, key, "placeholder")
            if tag == "input" and (element.get("type") or "").lower() in INPUT_VALUE_TYPES:
                self._attr_slot(element, tag, key, "value")
        elif tag == "meta" and (element.get("name") or "").strip().lower() == "description":
            self._attr_slot(element, tag, key, "content")

    def _attr_slot(self, element: Any, tag: str, key: str, name: str) -> None:
        raw = element.get(name)
        if raw is None:
            return
        value = norm(raw)
        if len(value) <= MAX_VALUE_LENGTH:
            self.slots.append(Slot(f"{key}/@{name}", "attr", value, tag, f"@{name}", element=element))


def _step(node: Any) -> str:
    """Étape relative au format de ``getpath`` : ``tag`` ou ``tag[n]`` si homonymes."""
    parent = node.getparent()
    if parent is None:
        return node.tag
    same = [sibling for sibling in parent if sibling.tag == node.tag]
    return node.tag if len(same) == 1 else f"{node.tag}[{same.index(node) + 1}]"


def _image_source(element: Any) -> Optional[str]:
    """Source affichée : ``data-src`` d'une image chargée à la demande, sinon ``src``."""
    src = (element.get("src") or "").strip()
    lazy = (element.get("data-src") or "").strip()
    value = lazy if lazy and (not src or src.lower().startswith("data:")) else src
    if not value or value.lower().startswith("data:") or len(value) > MAX_URL_LENGTH:
        return None
    return value


# ---------------------------------------------------------------------------
# Page : extraction, hydratation, rendu
# ---------------------------------------------------------------------------

class Page:
    """Une page du gabarit, analysée une fois puis hydratée et rendue."""

    def __init__(self, rel_path: str, text: str, encoding: str) -> None:
        self.rel_path = rel_path
        self.encoding = encoding
        self.fragment = not _DOCUMENT_TAG.search(text[:8192])
        root = etree.fromstring(text, _parser()) if text.strip() else None
        if root is None:
            raise ValueError(f"{rel_path} : document HTML vide ou illisible")
        self.root = root
        self.tree = root.getroottree()
        extractor = _Extractor(root)
        extractor.walk(root)
        self.slots = extractor.slots
        self.by_key = {slot.key: slot for slot in self.slots}

    @classmethod
    def parse(cls, raw: bytes, rel_path: str) -> "Page":
        text, encoding = decode_html(raw)
        return cls(rel_path, text, encoding)

    def inventory(self) -> list[dict[str, str]]:
        return [slot.inventory_entry() for slot in self.slots]

    # --- hydratation ------------------------------------------------------
    def apply(self, values: Mapping[str, Any]) -> ApplyReport:
        """Injecte les valeurs du client ; les clés inconnues ou valeurs refusées sont ignorées.

        Une valeur que lxml refuserait malgré le filtrage (ValueError) est comptée comme
        refusée : une seule valeur ne doit jamais empêcher la page d'être servie.
        """
        report = ApplyReport()
        for key, raw_value in values.items():
            slot = self.by_key.get(key)
            if slot is None:
                report.unknown.append(key)
                continue
            try:
                accepted = _apply_slot(slot, raw_value)
            except ValueError:
                accepted = False
            if accepted:
                report.applied += 1
            else:
                report.rejected.append(key)
        return report

    def current_value(self, slot: Slot) -> str:
        element = slot.element
        if slot.kind == "image":
            return _image_source(element) or ""
        if slot.kind == "attr":
            return norm(element.get(slot.position[1:]))
        text = _text_at(element, slot.position)
        return text if slot.preformatted else norm(text)

    # --- rendu ------------------------------------------------------------
    def render(self, *, tracker: str = "", bridge_src: Optional[str] = None) -> RenderedPage:
        """Page publique (+ carte d'édition) ; à n'appeler qu'une fois, après ``apply``."""
        if self.fragment:
            return RenderedPage(encode_html(self._serialize_fragment(), self.encoding), None, None)

        for node in self.root.iter():
            if isinstance(node.tag, str):
                node.attrib.pop(ANNOTATION_ATTR, None)
        numbers: dict[Any, int] = {}
        entries = []
        for slot in self.slots:
            number = numbers.get(slot.element)
            if number is None:
                number = numbers[slot.element] = len(numbers) + 1
                slot.element.set(ANNOTATION_ATTR, str(number))
            entry = {"n": number, "k": slot.key, "t": slot.kind, "p": slot.position, "g": slot.tag,
                     "x": self.current_value(slot)}
            if slot.preformatted:
                entry["pre"] = True
            entries.append(entry)
        map_json = json.dumps({"v": ENGINE_VERSION, "file": self.rel_path, "slots": entries},
                              ensure_ascii=False, separators=(",", ":"))
        map_version = hashlib.sha256(map_json.encode("utf-8")).hexdigest()[:12]

        html = etree.tostring(self.tree, method="html", encoding="unicode")
        snippets = []
        if tracker and TRACKER_MARKER not in html:
            snippets.append(tracker.strip())
        if bridge_src and LOADER_MARKER not in html:
            snippets.append(loader_snippet(bridge_src, self.rel_path, map_url(self.rel_path, map_version)))
        html = inject_before_body_end(html, "\n".join(snippets))
        return RenderedPage(encode_html(html, self.encoding), map_json, map_version)

    def _serialize_fragment(self) -> str:
        """Fragment (inclus par JavaScript) : réécrit sans l'enveloppe html/head/body implicite,
        avec tout ce que le parseur y a rangé — styles, scripts et commentaires de tête compris."""
        def node(item: Any) -> str:
            return etree.tostring(item, method="html", encoding="unicode", with_tail=False)

        parts = [node(item) for item in reversed(list(self.root.itersiblings(preceding=True)))]
        for name in ("head", "body"):
            container = self.root.find(name)
            if container is None:
                continue
            if container.text:
                parts.append(html_lib.escape(container.text, quote=False))
            parts.extend(etree.tostring(child, method="html", encoding="unicode") for child in container)
        parts.extend(node(item) for item in self.root.itersiblings())
        return "".join(parts)


def _apply_slot(slot: Slot, raw_value: Any) -> bool:
    if slot.kind == "image":
        url = safe_image_url(raw_value)
        if url is None:
            return False
        _set_image(slot.element, url)
        return True
    value = clean_text(raw_value)
    if value is None:
        return False
    if slot.kind == "text":
        _set_text(slot, value)
    else:
        slot.element.set(slot.position[1:], norm(value))
    return True


def _text_at(element: Any, position: str) -> str:
    index = int(position[1:])
    if index == 0:
        return element.text or ""
    return element[index - 1].tail or ""


def _set_text(slot: Slot, value: str) -> None:
    element, index = slot.element, int(slot.position[1:])
    current = _text_at(element, slot.position)
    if slot.preformatted:
        new = value
    else:
        new = (_LEADING_BLANKS.match(current).group(0) + norm(value)
               + _TRAILING_BLANKS.search(current).group(0))
    if index == 0:
        element.text = new
    else:
        element[index - 1].tail = new


def _set_image(element: Any, url: str) -> None:
    """Remplace l'image affichée, y compris ses variantes (srcset, <picture>, chargement différé)."""
    element.set("src", url)
    if element.get("data-src") is not None:
        element.set("data-src", url)
    for name in ("srcset", "sizes", "data-srcset", "data-sizes"):
        element.attrib.pop(name, None)
    picture = next((node for node in element.iterancestors() if _tag(node) == "picture"), None)
    if picture is not None:
        candidate = url.replace(",", "%2C")
        for source in picture.iter("source"):
            source.set("srcset", candidate)
            for name in ("type", "sizes", "data-srcset"):
                source.attrib.pop(name, None)


# ---------------------------------------------------------------------------
# Scripts injectés
# ---------------------------------------------------------------------------

def _js_string(value: str) -> str:
    """Littéral JS sûr dans un <script> : JSON, et « </ » neutralisé."""
    return json.dumps(value).replace("</", "<\\/")


def map_url(rel_path: str, version: str) -> str:
    return f"{ASSETS_PREFIX}/map/{quote(rel_path)}.json?v={version}"


def tracker_snippet(endpoint: str) -> str:
    """Micro-tracker sans cookie ; muet dans l'aperçu du Studio (?waas_edit=…)."""
    return f"""<!-- {TRACKER_MARKER} (Zero-Cookie / Ephemeral Daily Hash) -->
<script>
(function () {{
  try {{
    if (/[?&]waas_edit=/.test(window.location.search)) return;
    var payload = JSON.stringify({{ path: window.location.pathname }});
    var endpoint = {_js_string(endpoint)};
    if (navigator.sendBeacon) {{
      navigator.sendBeacon(endpoint, new Blob([payload], {{ type: 'application/json' }}));
    }} else {{
      fetch(endpoint, {{ method: 'POST', body: payload, keepalive: true, headers: {{ 'Content-Type': 'application/json' }} }});
    }}
  }} catch (e) {{}}
}})();
</script>"""


def loader_snippet(bridge_src: str, rel_path: str, map_src: str) -> str:
    """Chargeur du pont d'édition : rien n'est téléchargé hors de l'aperçu encadré du Studio."""
    return f"""<!-- {LOADER_MARKER} -->
<script>
(function () {{
  try {{
    if (window.top === window.self || !/[?&]waas_edit=(?:true|1)(?:&|$)/.test(window.location.search)) return;
    var s = document.createElement('script');
    s.src = {_js_string(bridge_src)};
    s.async = true;
    s.setAttribute('data-waas-file', {_js_string(rel_path)});
    s.setAttribute('data-waas-map', {_js_string(map_src)});
    (document.head || document.documentElement).appendChild(s);
  }} catch (e) {{}}
}})();
</script>"""


def inject_before_body_end(html: str, snippet: str) -> str:
    if not snippet:
        return html
    if _BODY_CLOSE.search(html):
        return _BODY_CLOSE.sub(lambda _match: snippet + "\n</body>", html, count=1)
    return f"{html}\n{snippet}\n"
