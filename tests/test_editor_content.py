"""Éditeur visuel v2 — modèle de contenu et réconciliation (fonctions pures).

Couvre l'extraction exhaustive des textes (y compris ceux que le moteur v1
ignorait), l'hydratation sans duplication ni perte d'espaces, la conservation du
DOCTYPE, le rendu public (annotations courtes, carte d'édition, chargeur du pont
limité au Studio), la compatibilité des clés historiques et chaque cas de la
réconciliation (gabarit réorganisé, retouché, éléments orphelins).

Exécution :

    .venv\\Scripts\\python.exe tests\\test_editor_content.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "End"))
sys.path.insert(0, str(ROOT / "waas-base-image"))

import content_sync  # noqa: E402
import waas_content as wc  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(f"{name} {detail}")
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f" -> {detail}" if detail and not condition else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


TEMPLATE = """<!DOCTYPE html>
<html lang="fr">
<head><meta charset="utf-8"><title>Boulangerie Dupont</title>
<meta name="description" content="Pains au levain depuis 1987"></head>
<body>
  <header><nav><a href="/">Accueil</a><a href="/contact.html" class="btn"><svg viewBox="0 0 24 24"><path d="M0 0"/></svg> Nous contacter</a></nav></header>
  <section class="hero">
    <h1>Le pain <span class="accent">vivant</span> du quartier</h1>
    <p><strong>Ouvert 7j/7</strong> de 6h à 20h, rue du Four.</p>
    <p>Ligne une<br>Ligne deux</p>
    <div class="stat">250+</div>
    <button type="button"><i class="icon"></i> Réserver</button>
    <picture><source srcset="hero.avif" type="image/avif"><img src="hero.jpg" srcset="hero-2x.jpg 2x" alt="Vitrine"></picture>
  </section>
  <table><tr><td>Baguette</td><td>1,20 €</td></tr></table>
  <figure><img src="four.jpg" alt="Four"><figcaption>Notre four à bois</figcaption></figure>
  <form><input type="email" placeholder="Votre e-mail"><input type="submit" value="Envoyer"></form>
  <p>Prix : 10&nbsp;€ <!-- promo --> seulement</p>
  <pre>  ligne 1
  ligne 2</pre>
  <script>var x = "Ne pas extraire";</script>
  <div data-waas-skip><p>Contenu exclu par l'auteur</p></div>
  <p>{{ client.nom }}</p>
  <p>·</p>
  <footer><p>© 2026 Boulangerie Dupont</p></footer>
</body>
</html>
"""


def page_of(html: str, rel: str = "index.html") -> wc.Page:
    return wc.Page.parse(html.encode("utf-8"), rel)


def slot(page: wc.Page, source: str) -> wc.Slot:
    return next(item for item in page.slots if item.source == source)


# ---------------------------------------------------------------------------
# 1. Extraction
# ---------------------------------------------------------------------------

def test_extraction() -> None:
    section("1. Extraction : tous les textes visibles, rien d'autre")
    page = page_of(TEMPLATE)
    sources = {item.source for item in page.slots}

    for text in ("Le pain", "vivant", "du quartier", "Ouvert 7j/7", "de 6h à 20h, rue du Four.",
                 "Ligne une", "Ligne deux", "250+", "Réserver", "Nous contacter", "Baguette",
                 "1,20 €", "Notre four à bois", "© 2026 Boulangerie Dupont"):
        check(f"texte extrait : {text!r}", text in sources)
    check("texte après un commentaire extrait", "seulement" in sources)
    check("espace insécable conservé dans la valeur", "Prix : 10\u00a0€" in sources, str(sorted(sources))[:200])
    check("titre de la page extrait", slot(page, "Boulangerie Dupont").key == "/html/head/title")
    check("description extraite (attribut)", slot(page, "Pains au levain depuis 1987").kind == "attr")
    check("texte alternatif extrait", slot(page, "Vitrine").key.endswith("/@alt"))
    check("texte d'aide d'un champ extrait", slot(page, "Votre e-mail").position == "@placeholder")
    check("libellé d'un bouton de formulaire extrait", slot(page, "Envoyer").position == "@value")
    images = [item for item in page.slots if item.kind == "image"]
    check("images extraites", {item.source for item in images} == {"hero.jpg", "four.jpg"})

    for excluded in ("Ne pas extraire", "Contenu exclu par l'auteur", "{{ client.nom }}", "·"):
        check(f"exclu : {excluded!r}", excluded not in sources)
    check("aucune clé dupliquée", len({item.key for item in page.slots}) == len(page.slots))

    pre = next(item for item in page.slots if item.preformatted)
    check("<pre> : texte brut conservé", pre.source == "  ligne 1\n  ligne 2", repr(pre.source))

    tail = slot(page, "du quartier")
    check("clé d'un texte de suite (XPath évaluable)",
          tail.key == "/html/body/section/h1/text()[normalize-space()][2]", tail.key)
    check("position : suite du 1er enfant (<span>)", tail.position == "t1", tail.position)
    resolved = page.root.xpath(tail.key)
    check("la clé désigne bien ce nœud texte", [wc.norm(value) for value in resolved] == ["du quartier"],
          str(resolved))
    check("clé d'un texte de tête (forme historique)", slot(page, "Le pain").key == "/html/body/section/h1")
    check("bouton à icône : position après l'icône", slot(page, "Réserver").position == "t1")

    anchored = page_of('<html><body><div data-waas-id="hero"><h2>Titre</h2><p>A</p><p>B</p></div>'
                       '<div data-waas-id="x"></div><div data-waas-id="x"><p>Doublon</p></div></body></html>')
    check("ancrage sur data-waas-id", slot(anchored, "Titre").key == "//*[@data-waas-id='hero']/h2")
    check("ancrage : frères homonymes indexés", slot(anchored, "B").key == "//*[@data-waas-id='hero']/p[2]")
    check("identifiant dupliqué ignoré (chemin absolu)", slot(anchored, "Doublon").key.startswith("/html/"))


def test_legacy_keys() -> None:
    section("2. Compatibilité : les clés du moteur v1 restent valides")
    os.environ.update({"TENANT_ID": "t", "SYNC_TOKEN": "x", "API_BASE_URL": "http://127.0.0.1:9"})
    backup = ROOT / "tests" / "fixtures" / "sync_engine_v1.py"
    check("moteur v1 archivé pour la non-régression", backup.is_file(), str(backup))
    if not backup.is_file():
        return
    spec = importlib.util.spec_from_file_location("sync_engine_v1", backup)
    legacy = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(legacy)
    # Gabarit sans « data-waas-skip » : cette exclusion volontaire (nouvelle) détache
    # à dessein les anciens enregistrements de la zone exclue.
    template = TEMPLATE.replace("<div data-waas-skip><p>Contenu exclu par l'auteur</p></div>", "")
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "index.html"
        path.write_text(template, encoding="utf-8")
        legacy_records = legacy.extract_content_from_html(str(path), "index.html")
    check("le moteur v1 a bien extrait des enregistrements", len(legacy_records) >= 5, str(len(legacy_records)))
    page = page_of(template)
    keys = {item.key: item for item in page.slots}
    missing = [r["xpath"] for r in legacy_records
               if r["xpath"] not in keys and wc.is_editable_text(wc.norm(r["content_value"]))]
    check("toute clé v1 utile existe en v2", not missing, str(missing))
    mismatched = [r["xpath"] for r in legacy_records
                  if r["xpath"] in keys and not wc.slot_matches(keys[r["xpath"]], r["content_value"])]
    check("même valeur d'origine sous la même clé", not mismatched, str(mismatched))


# ---------------------------------------------------------------------------
# 3. Hydratation et rendu
# ---------------------------------------------------------------------------

def test_hydration() -> None:
    section("3. Hydratation : ni duplication, ni perte d'espaces, ni DOCTYPE perdu")
    page = page_of(TEMPLATE)
    report = page.apply({
        slot(page, "Le pain").key: "Le levain",
        slot(page, "du quartier").key: "de la ville",
        slot(page, "hero.jpg").key: "https://cdn.example.com/hero.webp",
        slot(page, "four.jpg").key: "javascript:alert(1)",
        slot(page, "Vitrine").key: "Notre vitrine",
        "/html/body/inconnu": "x",
        slot(page, "Réserver").key: "Commander\x00",
    })
    check("valeurs appliquées", report.applied == 5, str(report))
    check("clé inconnue signalée", report.unknown == ["/html/body/inconnu"])
    check("URL javascript: refusée", report.rejected == [slot(page, "four.jpg").key])

    rendered = page.render(tracker=wc.tracker_snippet("https://api.example.com/waas/analytics/hit/demo"),
                           bridge_src="/__waas/editor-bridge.js?v=abc")
    html = rendered.html.decode("utf-8")
    check("DOCTYPE conservé", html.startswith("<!DOCTYPE html>"), html[:40])
    h1 = html[html.find("<h1"):html.find("</h1>") + 5]
    check("texte de tête remplacé, espace avant l'enfant conservé", "Le levain <span" in h1, h1)
    check("enfant intact, aucune duplication", h1.count("vivant") == 1 and "de la ville</h1>" in h1, h1)
    check("caractère de contrôle retiré", "Commander</button>" in html.replace("\n", ""))
    picture = html[html.find("<picture"):html.find("</picture>")]
    check("image remplacée, srcset retiré", 'src="https://cdn.example.com/hero.webp"' in picture
          and "hero-2x" not in picture, picture)
    check("<source> du <picture> neutralisée", 'srcset="https://cdn.example.com/hero.webp"' in picture
          and "image/avif" not in picture, picture)
    check("attribut alt remplacé", 'alt="Notre vitrine"' in picture)
    check("image refusée laissée intacte", 'src="four.jpg"' in html)

    check("aucun ancien attribut data-waas-xpath public", "data-waas-xpath" not in html)
    check("annotations courtes numérotées", 'data-waas="1"' in html)
    check("tracker injecté une fois", html.count(wc.TRACKER_MARKER) == 1)
    check("tracker muet dans l'aperçu du Studio", "waas_edit=" in html.split(wc.TRACKER_MARKER)[1][:400])
    loader = html.split(wc.LOADER_MARKER)[1][:700]
    check("chargeur du pont : uniquement encadré et en mode édition",
          "window.top === window.self" in loader and "waas_edit" in loader, loader)
    check("chargeur : carte versionnée", f"/__waas/map/index.html.json?v={rendered.map_version}" in loader)

    data = json.loads(rendered.map_json)
    entries = {entry["k"]: entry for entry in data["slots"]}
    check("carte : une entrée par emplacement", len(entries) == len(page.slots))
    check("carte : valeur publiée", entries[slot(page, "Le pain").key]["x"] == "Le levain")
    check("carte : position du texte de suite", entries[slot(page, "du quartier").key]["p"] == "t1")
    check("carte : élément partagé par ses emplacements",
          entries[slot(page, "Le pain").key]["n"] == entries[slot(page, "du quartier").key]["n"])

    versions = []
    for _ in range(2):
        fresh = page_of(TEMPLATE)
        fresh.apply({slot(fresh, "Le pain").key: "Le levain"})
        versions.append(fresh.render(bridge_src="/b.js").map_version)
    check("rendu déterministe (même contenu, même version de carte)", versions[0] == versions[1], str(versions))
    untouched = page_of(TEMPLATE).render(bridge_src="/b.js").map_version
    check("la version de carte change avec le contenu publié", untouched != versions[0])


def test_documents() -> None:
    section("4. Documents particuliers")
    no_doctype = page_of("<html><body><p>Texte</p></body></html>").render().html.decode("utf-8")
    check("aucun DOCTYPE inventé (le mode de rendu ne change pas)", not no_doctype.lower().startswith("<!doctype"))

    fragment = page_of("<div class='card'><h3>Carte</h3> suite</div>", "partials/card.html")
    check("fragment : textes extraits", {"Carte", "suite"} <= {item.source for item in fragment.slots})
    out = fragment.render(tracker="<script>t</script>", bridge_src="/b.js")
    text = out.html.decode("utf-8")
    check("fragment réécrit sans enveloppe html/body ni scripts",
          text.startswith("<div") and "<body" not in text and "<script" not in text, text)
    check("fragment : pas de carte d'édition", out.map_json is None)

    latin = "<html><head><meta charset='iso-8859-1'></head><body><p>Crème brûlée</p></body></html>"
    page = wc.Page.parse(latin.encode("latin-1"), "latin.html")
    check("encodage déclaré respecté à la lecture", "Crème brûlée" in {item.source for item in page.slots})
    page.apply({page.slots[0].key: "Tarte à l'œuf"})
    raw = page.render().html
    check("réécrit dans l'encodage d'origine (inencodable -> référence)",
          "Tarte à l'".encode("latin-1") in raw and b"&#339;" in raw, raw[-80:].decode("latin-1"))

    partial = wc.Page.parse(b"<!-- en-tete -->\n<style>.x{color:red}</style><script>init()</script>"
                            b"<div class='x'>Bonjour</div>", "partials/head.html")
    text = partial.render().html.decode("utf-8")
    check("fragment : commentaire, style et script de tête conservés",
          text.startswith("<!-- en-tete -->") and "<style>.x{color:red}</style>" in text
          and "<script>init()</script>" in text and text.rstrip().endswith("</div>"), text)

    hostile = page_of("<html><body><h1>Titre</h1><img src='a.jpg' alt='x'><pre>  a\n  b</pre></body></html>")
    report = hostile.apply({"/html/body/h1": "A￾B￿", "/html/body/img/@alt": "x\ud800",
                            "/html/body/img": "https://cdn.example.com/a￾.jpg"})
    check("non-caractères retirés du texte (pas de plantage de lxml)", report.applied == 2
          and "<h1 data-waas=\"1\">AB</h1>" in hostile.render().html.decode("utf-8"), str(report))
    check("adresse d'image avec non-caractère refusée", report.rejected == ["/html/body/img"], str(report))
    pre = next(item for item in hostile.slots if item.preformatted)
    check("texte préformaté : comparaison exacte (inventaire)", pre.inventory_entry().get("exact") is True)
    check("texte préformaté : un changement d'indentation compte", not wc.slot_matches(pre, "a\n  b"))

    lazy = page_of('<html><body><img src="data:image/gif;base64,R0lG" data-src="photo.jpg" class="lazyload"></body></html>')
    image = next(item for item in lazy.slots if item.kind == "image")
    check("image différée : source réelle (data-src)", image.source == "photo.jpg")
    lazy.apply({image.key: "https://cdn.example.com/p.jpg"})
    html = lazy.render().html.decode("utf-8")
    check("image différée : src et data-src remplacés",
          html.count("https://cdn.example.com/p.jpg") == 2, html)


# ---------------------------------------------------------------------------
# 5. Réconciliation
# ---------------------------------------------------------------------------

def inv(*pairs: tuple[str, str], kind: str = "text") -> list[content_sync.InventorySlot]:
    return [content_sync.InventorySlot(key, kind, source) for key, source in pairs]


def rec(key: str, value: str, source: str | None, kind: str = "text") -> content_sync.StoredRecord:
    return content_sync.StoredRecord(key, kind, value, source)


def test_reconcile() -> None:
    section("5. Réconciliation des enregistrements avec le gabarit")
    slots = inv(("/h1", "Bienvenue"), ("/p[1]", "Intro"), ("/p[2]", "Contact"))

    plan = content_sync.reconcile(slots, [], anchors=True)
    check("premier démarrage : tout est indexé", [s.key for s in plan.inserts] == ["/h1", "/p[1]", "/p[2]"])
    check("premier démarrage : rien à injecter", plan.placements == {})

    plan = content_sync.reconcile(slots, [rec("/h1", "Chez Dupont", "Bienvenue"),
                                          rec("/p[1]", "Intro", "Intro")], anchors=True)
    check("à sa place : seule la valeur modifiée est injectée", plan.placements == {"/h1": "Chez Dupont"})
    check("à sa place : seul le nouvel emplacement est indexé", [s.key for s in plan.inserts] == ["/p[2]"])
    check("aucune écriture superflue", plan.writes == 1)

    plan = content_sync.reconcile(slots, [rec("/h1", "Chez Dupont", None)], anchors=True)
    check("historique sans ancre : ancre complétée", plan.backfills == [("/h1", "Bienvenue")])
    check("historique : valeur appliquée", plan.placements.get("/h1") == "Chez Dupont")

    shifted = inv(("/h1", "Bienvenue"), ("/p[1]", "Bandeau promo"), ("/p[2]", "Intro"), ("/p[3]", "Contact"))
    plan = content_sync.reconcile(shifted, [
        rec("/h1", "Bienvenue", "Bienvenue"),
        rec("/p[1]", "Mon intro", "Intro"),
        rec("/p[2]", "Écrivez-nous", "Contact"),
    ], anchors=True)
    check("gabarit réorganisé : les clés suivent le texte",
          sorted(plan.moves) == [("/p[1]", "/p[2]"), ("/p[2]", "/p[3]")], str(plan.moves))
    check("gabarit réorganisé : valeurs au bon endroit",
          plan.placements == {"/p[2]": "Mon intro", "/p[3]": "Écrivez-nous"}, str(plan.placements))
    check("gabarit réorganisé : nouvel élément indexé", [s.key for s in plan.inserts] == ["/p[1]"])

    swapped = inv(("/p[1]", "B"), ("/p[2]", "A"))
    plan = content_sync.reconcile(swapped, [rec("/p[1]", "a!", "A"), rec("/p[2]", "b!", "B")], anchors=True)
    check("permutation de deux éléments", sorted(plan.moves) == [("/p[1]", "/p[2]"), ("/p[2]", "/p[1]")])
    check("permutation : valeurs croisées", plan.placements == {"/p[1]": "b!", "/p[2]": "a!"})

    retouched = inv(("/h1", "Bienvenue chez nous"))
    plan = content_sync.reconcile(retouched, [rec("/h1", "Chez Dupont", "Bienvenu")], anchors=True)
    check("gabarit retouché : la personnalisation du client prime",
          plan.reanchors == [("/h1", "Bienvenue chez nous", None)] and plan.placements == {"/h1": "Chez Dupont"},
          f"{plan.reanchors} {plan.placements}")
    plan = content_sync.reconcile(retouched, [rec("/h1", "Bienvenu", "Bienvenu")], anchors=True)
    check("gabarit retouché : une valeur jamais modifiée suit le gabarit",
          plan.reanchors == [("/h1", "Bienvenue chez nous", "Bienvenue chez nous")] and not plan.placements)

    plan = content_sync.reconcile(inv(("/h1", "Titre")), [
        rec("/h2", "Sous-titre réécrit", "Sous-titre"),
        rec("/h3", "Défaut", "Défaut"),
        rec("orphan:20260101T000000Z:/h4", "ancien", "x"),
    ], anchors=True)
    check("orphelin personnalisé : détaché (récupérable)", plan.detach == ["/h2"])
    check("orphelin par défaut : supprimé", plan.delete == ["/h3"])
    check("contenu déjà détaché : ignoré", "orphan:20260101T000000Z:/h4" not in plan.detach + plan.delete)

    ambiguous = inv(("/a[1]", "En savoir plus"), ("/a[2]", "En savoir plus"))
    plan = content_sync.reconcile(ambiguous, [rec("/a[3]", "Découvrir", "En savoir plus")], anchors=True)
    check("texte d'origine ambigu : pas de déplacement hasardeux", not plan.moves and plan.detach == ["/a[3]"])

    plan = content_sync.reconcile(slots, [rec("/h1", "Chez Dupont", None), rec("/ancien", "x", None)],
                                  anchors=False)
    check("sans migration : comportement v1 (par clé)", plan.placements == {"/h1": "Chez Dupont"})
    check("sans migration : aucune ancre, aucun détachement",
          not (plan.backfills or plan.detach or plan.delete or plan.moves))

    preformatted = [content_sync.InventorySlot("/pre", "text", "  a\n  b", exact=True)]
    plan = content_sync.reconcile(preformatted, [rec("/pre", "a\n  b", "  a\n  b")], anchors=True)
    check("texte préformaté : un changement de blancs est publié", plan.placements == {"/pre": "a\n  b"})

    many = inv(*[(f"/p[{i}]", f"T{i}") for i in range(1, 11)])
    plan = content_sync.reconcile(many, [rec(f"/p[{i}]", f"T{i}", None) for i in range(1, 11)],
                                  anchors=True, max_backfills=4)
    check("mise à niveau bornée par appel (le reste au démarrage suivant)", len(plan.backfills) == 4)

    plan = content_sync.reconcile(slots, [content_sync.StoredRecord("/h1", "text", "x", "Ancien", id=42)],
                                  anchors=True)
    check("plan : identifiant de ligne retenu pour garder les écritures", plan.ids == {"/h1": 42})

    images = inv(("/img", "a.jpg"), kind="image")
    plan = content_sync.reconcile(images, [rec("/img", " a.jpg ", "a.jpg", kind="image")], anchors=True)
    check("image identique (aux blancs près) : rien à injecter", not plan.placements)
    plan = content_sync.reconcile(images, [rec("/img", "b.jpg", "a.jpg", kind="text")], anchors=True)
    check("type incompatible sous la même clé : jamais appliqué", "/img" not in plan.placements)


def test_validation() -> None:
    section("6. Validation des valeurs (API)")
    ok = content_sync.validate_value
    check("texte accepté", ok("text", "Bonjour") == ("Bonjour", None))
    check("contrôles retirés", ok("text", "A\x00B\x07C\nD") == ("ABC\nD", None))
    check("texte trop long refusé", ok("text", "x" * 5001)[0] is None)
    check("image https acceptée", ok("image", " https://cdn.example.com/a.png ")[0] == "https://cdn.example.com/a.png")
    check("image relative acceptée", ok("image", "img/a.png")[0] == "img/a.png")
    for bad in ("javascript:alert(1)", "data:image/png;base64,AAA", "vbscript:x", 'a.png" onerror="x',
                "file:///etc/passwd", "", "a b.png"):
        check(f"image refusée : {bad!r}", ok("image", bad)[0] is None)
    check("valeur non textuelle refusée", ok("text", 42)[0] is None)
    check("non-caractères retirés (la publication ne peut plus échouer)", ok("text", "A￾B￿\ud800") == ("AB", None))
    check("image avec non-caractère refusée", ok("image", "https://a.b/c￾.jpg")[0] is None)


def test_studio_model_js() -> None:
    section("7. Studio : modèle JavaScript (Node)")
    node = shutil.which("node")
    if not node:
        print("  (Node absent : tests JavaScript ignorés)")
        return
    run = subprocess.run([node, str(ROOT / "tests" / "js" / "studio_model.test.js")],
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    summary = [line for line in run.stdout.splitlines() if "vérifications réussies" in line]
    failed = [line.strip() for line in run.stdout.splitlines() if "[FAIL]" in line]
    check(f"tests du modèle du Studio ({summary[-1].strip() if summary else 'sans résultat'})",
          run.returncode == 0, "; ".join(failed) or run.stderr[-300:])

    run_heal = subprocess.run([node, str(ROOT / "tests" / "js" / "dom_healing.test.js")],
                              capture_output=True, text=True, encoding="utf-8", timeout=60)
    summary_heal = [line for line in run_heal.stdout.splitlines() if "vérifications réussies" in line]
    failed_heal = [line.strip() for line in run_heal.stdout.splitlines() if "[FAIL]" in line]
    check(f"tests de résilience DOM / healing ({summary_heal[-1].strip() if summary_heal else 'sans résultat'})",
          run_heal.returncode == 0, "; ".join(failed_heal) or run_heal.stderr[-300:])


# ---------------------------------------------------------------------------

def main_test() -> int:
    print("Éditeur visuel v2 : modèle de contenu et réconciliation")
    test_extraction()
    test_legacy_keys()
    test_hydration()
    test_documents()
    test_reconcile()
    test_validation()
    test_studio_model_js()

    print(f"\n{'=' * 60}")
    print(f"{CHECKS - len(FAILURES)}/{CHECKS} vérifications réussies")
    if FAILURES:
        print("ÉCHECS :")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("Modèle de contenu vérifié.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_test())
