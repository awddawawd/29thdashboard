/*
 * Studio — tests du modèle (Front/js/studio/studio-model.js), sans dépendance.
 * Exécution : node tests/js/studio_model.test.js  (lancé aussi par tests/test_editor_content.py)
 * 29th studio · © 2026
 */
'use strict';

const assert = require('assert');
const path = require('path');

global.window = {};
require(path.join(__dirname, '..', '..', 'Front', 'js', 'studio', 'studio-model.js'));
const SM = global.window.StudioModel;

let passed = 0;
const failures = [];
function check(name, run) {
  try {
    run();
    passed += 1;
    console.log(`  [PASS] ${name}`);
  } catch (error) {
    failures.push(name);
    console.log(`  [FAIL] ${name} -> ${error.message}`);
  }
}

const row = (file, xpath, value, extra) => Object.assign(
  { id: `${file}|${xpath}`, file_path: file, xpath, content_type: 'text', content_value: value }, extra || {});

function freshModel() {
  const model = SM.createModel();
  model.load({
    anchors: true,
    content: [
      row('index.html', '/html/head/title', 'Accueil — Dupont'),
      row('index.html', '/html/body/h1', 'Le pain', { source_value: 'Le pain' }),
      row('index.html', '/html/body/h1/text()[normalize-space()][2]', 'du quartier'),
      row('index.html', '/html/body/img', 'hero.jpg', { content_type: 'image' }),
      row('index.html', '/html/body/img/@alt', 'Vitrine', { content_type: 'attr' }),
      row('about/index.html', '/html/body/h1', 'Notre histoire'),
      row('index.html', 'orphan:20260101T000000Z:/html/body/h2', 'Ancien')
    ]
  });
  return model;
}

const key = (file, xpath) => SM.keyOf(file, xpath);
const H1 = key('index.html', '/html/body/h1');

console.log('Studio : modèle');

check('pagePath : adresses des pages', () => {
  assert.strictEqual(SM.pagePath('index.html'), '/');
  assert.strictEqual(SM.pagePath('about/index.html'), '/about/');
  assert.strictEqual(SM.pagePath('contact.html'), '/contact.html');
  assert.strictEqual(SM.pagePath('dossier/page é.html'), '/dossier/page%20%C3%A9.html');
});

check('pagePath : un chemin ne peut jamais sortir de la vitrine', () => {
  ['/evil.example/x.html', '../x.html', 'a//b.html', 'a\\b.html', '', './x.html', 'a/\u0000.html'].forEach((file) => {
    assert.strictEqual(SM.pagePath(file), null, file);
  });
});

check('describeXPath : balise et attribut', () => {
  assert.deepStrictEqual(SM.describeXPath('/html/body/p[2]/text()[normalize-space()][3]'), { tag: 'p', attr: null });
  assert.deepStrictEqual(SM.describeXPath('/html/body/img/@alt'), { tag: 'img', attr: 'alt' });
  assert.deepStrictEqual(SM.describeXPath("//*[@data-waas-id='hero']"), { tag: '', attr: null });
  assert.deepStrictEqual(SM.describeXPath('orphan:20260101T000000Z:/html/body/h2'), { tag: 'h2', attr: null });
});

check('libellés et pages', () => {
  const model = freshModel();
  assert.strictEqual(model.item(H1).label, 'Titre principal · H1');
  assert.strictEqual(model.item(key('index.html', '/html/body/img/@alt')).label, 'Texte alternatif');
  assert.deepStrictEqual(model.pages(), ['index.html', 'about/index.html']);
  assert.strictEqual(model.pageTitle('index.html'), 'Accueil — Dupont');
  assert.strictEqual(model.pageTitle('about/index.html'), '/about/');
  assert.ok(model.item(key('index.html', 'orphan:20260101T000000Z:/html/body/h2')).detached);
});

check('saisie continue regroupée en une seule étape d\'annulation', () => {
  const model = freshModel();
  model.setValue(H1, 'Le p', { coalesce: true });
  model.setValue(H1, 'Le pa', { coalesce: true });
  model.setValue(H1, 'Le levain', { coalesce: true });
  assert.strictEqual(model.undo.length, 1);
  assert.deepStrictEqual(model.undoStep(), { key: H1, value: 'Le pain' });
  assert.strictEqual(model.dirtyCount(), 0);
  assert.deepStrictEqual(model.redoStep(), { key: H1, value: 'Le levain' });
  assert.strictEqual(model.current(H1), 'Le levain');
});

check('revenir à la valeur enregistrée retire la modification en attente', () => {
  const model = freshModel();
  model.setValue(H1, 'Le levain');
  model.setValue(H1, 'Le pain');
  assert.strictEqual(model.dirtyCount(), 0);
});

check('lot envoyé : valeur attendue = dernière valeur connue en base', () => {
  const model = freshModel();
  model.setValue(H1, 'Le levain');
  assert.deepStrictEqual(model.changes(), [{
    file_path: 'index.html', xpath: '/html/body/h1', new_value: 'Le levain', expected_value: 'Le pain'
  }]);
  assert.strictEqual('expected_value' in model.changes(new Set([H1]))[0], false);
});

check('résultats : la saisie faite pendant l\'envoi reste en attente', () => {
  const model = freshModel();
  model.setValue(H1, 'Le levain');
  const sent = model.changes();
  model.setValue(H1, 'Le levain doré');          // frappe pendant la requête
  model.applyResults([{ status: 'updated' }], sent);
  assert.strictEqual(model.item(H1).value, 'Le levain');
  assert.strictEqual(model.current(H1), 'Le levain doré');
  assert.strictEqual(model.dirtyCount(), 1);
});

check('résultats : un retour à l\'ancien texte pendant l\'envoi n\'est pas perdu', () => {
  const model = freshModel();
  model.setValue(H1, 'Le levain');
  const sent = model.changes();
  model.setValue(H1, 'Le pain');                 // annulé avant la réponse
  model.applyResults([{ status: 'updated' }], sent);
  assert.strictEqual(model.item(H1).value, 'Le levain');
  assert.strictEqual(model.current(H1), 'Le pain');
});

check('résultats : conflit, refus, conflit déjà identique', () => {
  const model = freshModel();
  const ALT = key('index.html', '/html/body/img/@alt');
  const IMG = key('index.html', '/html/body/img');
  model.setValue(H1, 'Le levain');
  model.setValue(ALT, 'Pains au levain');
  model.setValue(IMG, 'javascript:alert(1)');
  const sent = model.changes();
  const outcome = model.applyResults([
    { status: 'conflict', current_value: 'Version de Camille' },
    { status: 'conflict', current_value: 'Pains au levain' },
    { status: 'invalid', detail: 'Adresse refusée.' }
  ], sent);
  assert.deepStrictEqual(outcome.conflicts, [H1]);
  assert.deepStrictEqual(outcome.saved, [ALT]);
  assert.deepStrictEqual(outcome.failed, [IMG]);
  assert.strictEqual(model.problem(H1).current, 'Version de Camille');
  assert.strictEqual(model.problem(IMG).detail, 'Adresse refusée.');
  assert.strictEqual(model.isDirty(ALT), false);
});

check('conflit : adopter la version en ligne purge l\'historique de ce texte', () => {
  const model = freshModel();
  model.setValue(H1, 'Le levain');
  model.applyResults([{ status: 'conflict', current_value: 'Version de Camille' }], model.changes());
  model.adoptRemote(H1);
  assert.strictEqual(model.item(H1).value, 'Version de Camille');
  assert.strictEqual(model.dirtyCount(), 0);
  assert.strictEqual(model.undoStep(), null);
});

check('rechargement : la valeur de référence d\'une modification en attente est conservée', () => {
  const model = freshModel();
  model.setValue(H1, 'Le levain');
  model.load({ content: [row('index.html', '/html/body/h1', 'Changé ailleurs')] });
  assert.strictEqual(model.item(H1).value, 'Le pain');
  assert.strictEqual(model.changes()[0].expected_value, 'Le pain');
});

console.log(`\n${passed}/${passed + failures.length} vérifications réussies`);
process.exitCode = failures.length ? 1 : 0;
