import json
import re
from pathlib import Path
from typing import Any
from lxml import etree, html

# Balises à ignorer complètement lors de l'extraction
IGNORED_TAGS = {"script", "style", "noscript", "svg", "path", "meta", "link", "head"}

# Attributs éditables à cibler selon la balise
EDITABLE_ATTRIBUTES = {
    "img": ["src", "alt"],
    "a": ["href"],
    "video": ["src"],
    "source": ["src"],
}


def get_robust_xpath(element: html.HtmlElement, root: html.HtmlElement) -> str:
    """
    Génère un XPath robuste.
    Si l'élément ou un de ses parents proches possède un ID unique,
    on ancre le XPath dessus pour le rendre résistant aux réorganisations.
    """
    # 1. Ancrage sur ID direct
    elem_id = element.get("id")
    if elem_id and not re.search(r"\d{5,}", elem_id):  # Ignore les ID générés dynamiquement
        return f"//*[@id='{elem_id}']"

    # 2. Construction du chemin remontant jusqu'à un ancrage ou la racine
    path_components = []
    current = element

    while current is not None and current != root:
        parent = current.getparent()
        if parent is None:
            break

        tag = current.tag
        # Si un ancêtre a un ID unique, on s'arrête là
        ancestor_id = current.get("id")
        if ancestor_id and not re.search(r"\d{5,}", ancestor_id):
            path_components.append(f"*[@id='{ancestor_id}']")
            return "//" + "/".join(reversed(path_components))

        # Calcul de l'index parmi les frères du même type
        siblings = [sib for sib in parent if sib.tag == tag]
        if len(siblings) > 1:
            index = siblings.index(current) + 1
            path_components.append(f"{tag}[{index}]")
        else:
            path_components.append(f"{tag}")

        current = parent

    path_components.append("html")
    return "/" + "/".join(reversed(path_components))


def extract_content(html_path: str | Path) -> list[dict[str, Any]]:
    """
    Parse le document HTML et extrait les textes visibles et les médias éditables.
    """
    file_path = Path(html_path)
    if not file_path.exists():
        raise FileNotFoundError(f"Fichier introuvable : {file_path}")

    parser = html.HTMLParser(encoding="utf-8", remove_blank_text=True)
    tree = html.parse(str(file_path), parser=parser)
    root = tree.getroot()

    extracted_items = []

    for element in root.iter():
        # Ignorer les commentaires et balises non pertinentes
        if not isinstance(element.tag, str) or element.tag in IGNORED_TAGS:
            continue

        base_xpath = get_robust_xpath(element, root)

        # 1. Extraction du texte propre à la balise (sans enfants)
        direct_text = element.text
        if direct_text:
            cleaned_text = re.sub(r"\s+", " ", direct_text).strip()
            # On ignore les textes vides ou trop courts non significatifs
            if cleaned_text and len(cleaned_text) > 1:
                extracted_items.append({
                    "type": "text",
                    "tag": element.tag,
                    "xpath": base_xpath,
                    "target": "text",
                    "default_value": cleaned_text,
                })

        # 2. Extraction des attributs clés (ex: src pour img)
        if element.tag in EDITABLE_ATTRIBUTES:
            for attr in EDITABLE_ATTRIBUTES[element.tag]:
                val = element.get(attr)
                if val:
                    extracted_items.append({
                        "type": "attribute",
                        "tag": element.tag,
                        "xpath": f"{base_xpath}/@{attr}",
                        "target": attr,
                        "default_value": val.strip(),
                    })

    return extracted_items


if __name__ == "__main__":
    # Exemple d'exécution
    fichier_cible = "index.html"

    # Simulation si index.html n'existe pas encore
    if not Path(fichier_cible).exists():
        html_dummy = """
        <!DOCTYPE html>
        <html>
        <head><title>Boutique</title></head>
        <body>
            <header id="header-main">
                <h1>Mon Magasin</h1>
                <p id="slogan">Les meilleurs produits du coin.</p>
            </header>
            <main>
                <div class="banner">
                    <img id="hero-img" src="assets/banner.jpg" alt="Bannière d'accueil">
                    <p>Découvrez notre nouvelle collection disponible en rayon dès maintenant.</p>
                    <a href="https://catalogue.local/produits">Voir le catalogue</a>
                </div>
            </main>
        </body>
        </html>
        """
        Path(fichier_cible).write_text(html_dummy, encoding="utf-8")

    # Extraction et affichage
    donnees = extract_content(fichier_cible)
    print(json.dumps(donnees, indent=2, ensure_ascii=False))