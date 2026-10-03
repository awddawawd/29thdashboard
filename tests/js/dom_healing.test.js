/*
 * Studio — tests de résilience DOM (DOM Healing / Un-split), sans dépendance.
 * Exécution : node tests/js/dom_healing.test.js (lancé aussi par tests/test_editor_content.py)
 * 29th studio · © 2026
 */
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');

// Simple DOM Mock pour Node.js sans dépendances externes
class Node {
  constructor(nodeType, nodeValue = null) {
    this.nodeType = nodeType; // 1 = Element, 3 = Text
    this.nodeValue = nodeValue;
    this.childNodes = [];
    this.parentNode = null;
  }
  get isConnected() { return true; }
  getBoundingClientRect() { return this._box || { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
  getClientRects() { return this._rects || (this._box ? [this._box] : []); }
  get firstChild() { return this.childNodes[0] || null; }
  get nextSibling() {
    if (!this.parentNode) return null;
    const idx = this.parentNode.childNodes.indexOf(this);
    return idx >= 0 && idx + 1 < this.parentNode.childNodes.length ? this.parentNode.childNodes[idx + 1] : null;
  }
  get textContent() {
    if (this.nodeType === 3) return this.nodeValue || '';
    return this.childNodes.map(c => c.textContent).join('');
  }
  set textContent(val) {
    this.childNodes = [];
    if (val) this.appendChild(new Node(3, val));
  }
  appendChild(child) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }
  insertBefore(newChild, refChild) {
    if (newChild.parentNode) newChild.parentNode.removeChild(newChild);
    newChild.parentNode = this;
    const idx = this.childNodes.indexOf(refChild);
    if (idx === -1) this.childNodes.push(newChild);
    else this.childNodes.splice(idx, 0, newChild);
    return newChild;
  }
  removeChild(child) {
    const idx = this.childNodes.indexOf(child);
    if (idx !== -1) {
      this.childNodes.splice(idx, 1);
      child.parentNode = null;
    }
    return child;
  }
  contains(node) {
    let curr = node;
    while (curr) {
      if (curr === this) return true;
      curr = curr.parentNode;
    }
    return false;
  }
  normalize() {
    const merged = [];
    for (const child of this.childNodes) {
      if (child.nodeType === 3) {
        if (!child.nodeValue) continue;
        if (merged.length && merged[merged.length - 1].nodeType === 3) {
          merged[merged.length - 1].nodeValue += child.nodeValue;
        } else {
          merged.push(child);
        }
      } else {
        child.normalize();
        merged.push(child);
      }
    }
    this.childNodes = merged;
  }
}

class Element extends Node {
  constructor(tagName) {
    super(1);
    this.tagName = tagName.toUpperCase();
    this.attributes = new Map();
    this.classList = {
      _set: new Set(),
      add(...classes) { classes.forEach(c => this._set.add(c)); },
      remove(...classes) { classes.forEach(c => this._set.delete(c)); },
      contains(c) { return this._set.has(c); }
    };
    this.style = {
      _props: new Map(),
      getPropertyValue(p) { return this._props.get(p) || ''; },
      setProperty(p, v) { this._props.set(p, v); }
    };
  }
  get className() { return Array.from(this.classList._set).join(' '); }
  set className(val) {
    this.classList._set.clear();
    (val || '').split(/\s+/).filter(Boolean).forEach(c => this.classList.add(c));
  }
  setAttribute(k, v) { this.attributes.set(k, String(v)); }
  getAttribute(k) { return this.attributes.get(k) || null; }
  hasAttribute(k) { return this.attributes.has(k); }
  removeAttribute(k) { this.attributes.delete(k); }
  querySelectorAll(selector) {
    const results = [];
    const walk = (node) => {
      for (const child of node.childNodes) {
        if (child.nodeType === 1) {
          const tag = child.tagName.toLowerCase();
          const matchTag = selector.includes(tag);
          const matchAttr = selector.includes('[data-waas]') ? child.hasAttribute('data-waas') : false;
          if (matchTag || matchAttr) {
            results.push(child);
          }
          walk(child);
        }
      }
    };
    walk(this);
    return results;
  }
  closest(selector) {
    let curr = this;
    while (curr) {
      if (curr.nodeType === 1) {
        if (selector === 'a[href]' && curr.tagName === 'A' && curr.hasAttribute('href')) return curr;
        if (curr.hasAttribute('data-split') || curr.classList.contains('is-splitting')) return curr;
        if (curr.tagName && selector.toUpperCase().split(/[\s,]+/).includes(curr.tagName)) return curr;
      }
      curr = curr.parentNode;
    }
    return null;
  }
  attachShadow() { return new Element('shadow-root'); }
}

class DocumentMock {
  constructor() {
    this.body = new Element('body');
    this.documentElement = new Element('html');
    this.documentElement.appendChild(this.body);
  }
  createElement(tag) { return new Element(tag); }
  createElementNS(_ns, tag) { return new Element(tag); }
  createTextNode(text) { return new Node(3, text); }
  createRange() {
    return {
      selectNodeContents(node) { this.node = node; },
      getClientRects() {
        return this.node && this.node._rects ? this.node._rects : (this.node && this.node._box ? [this.node._box] : []);
      }
    };
  }
  querySelectorAll(selector) {
    const match = /\[data-waas="(\d+)"\]/.exec(selector);
    if (!match) return [];
    const num = match[1];
    const results = [];
    const walk = (node) => {
      if (node.nodeType === 1 && node.getAttribute('data-waas') === num) results.push(node);
      for (const child of node.childNodes) walk(child);
    };
    walk(this.documentElement);
    return results;
  }
}

global.document = new DocumentMock();
global.document.head = new Element('head');
global.document.currentScript = null;
global.window = {
  innerWidth: 1024, innerHeight: 768, requestAnimationFrame: () => {},
  addEventListener: () => {}, removeEventListener: () => {},
  location: { href: 'http://localhost:8000/', origin: 'http://localhost:8000', pathname: '/', assign: () => {} },
  open: () => {}
};
global.Node = Node;
global.Element = Element;

const vm = require('vm');
const domCode = fs.readFileSync(path.join(__dirname, '..', '..', 'waas-base-image', 'editor', '10-dom.js'), 'utf-8');
vm.runInThisContext(domCode);
const overlayCode = fs.readFileSync(path.join(__dirname, '..', '..', 'waas-base-image', 'editor', '20-overlay.js'), 'utf-8');
vm.runInThisContext(overlayCode);
const bridgeCode = fs.readFileSync(path.join(__dirname, '..', '..', 'waas-base-image', 'editor', '30-bridge.js'), 'utf-8');
vm.runInThisContext(bridgeCode);
global.state = state;

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

console.log('Pont : résilience et DOM healing (Node)');

check('Texte natif : reconnu sans altération', () => {
  const p = global.document.createElement('p');
  p.setAttribute('data-waas', '1');
  p.appendChild(global.document.createTextNode('Titre simple'));
  global.document.body.appendChild(p);

  const map = {
    slots: [
      { n: 1, k: '/html/body/p', t: 'text', p: 't0', g: 'p', x: 'Titre simple' }
    ]
  };
  buildSlots(map);
  const slot = global.state.byKey.get('/html/body/p');
  assert(slot, 'slot doit exister');
  assert.strictEqual(slot.ok, true, 'slot.ok doit valoir true');
});

check("Texte scindé par Splitting.js : réparé et reconnu (cas réel du client)", () => {
  global.state.slots = [];
  global.state.byKey.clear();
  global.state.byNumber.clear();

  // Création du paragraphe morcelé
  const p = global.document.createElement('p');
  p.className = 'approach__text is-splitting is-in';
  p.setAttribute('data-split', '');
  p.setAttribute('data-waas', '49');

  function makeWord(text) {
    const w = global.document.createElement('span');
    w.className = 'split-w';
    const win = global.document.createElement('span');
    win.className = 'split-w__in';
    win.appendChild(global.document.createTextNode(text));
    w.appendChild(win);
    return w;
  }

  // "Un site, une API, une automatisation : la forme change,"
  p.appendChild(makeWord('Un'));
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord('site,'));
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord('une'));
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord('API,'));
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord('une'));
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord('automatisation'));
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord(':'));
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord('la'));
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord('forme'));
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord('change,'));
  p.appendChild(global.document.createTextNode(' '));

  // <em data-waas="50">"pas l'exigence."</em>
  const em50 = global.document.createElement('em');
  em50.setAttribute('data-waas', '50');
  em50.appendChild(makeWord('pas'));
  em50.appendChild(global.document.createTextNode(' '));
  em50.appendChild(makeWord("l'exigence."));
  p.appendChild(em50);

  // " On conçoit des outils rapides, solides et simples à utiliser, puis on reste à vos côtés "
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord('On'));
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord('conçoit'));
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord('des'));
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord('outils'));
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord('rapides,'));
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord('solides'));
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord('et'));
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord('simples'));
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord('à'));
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord('utiliser,'));
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord('puis'));
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord('on'));
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord('reste'));
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord('à'));
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord('vos'));
  p.appendChild(global.document.createTextNode(' '));
  p.appendChild(makeWord('côtés'));
  p.appendChild(global.document.createTextNode(' '));

  // <em data-waas="51">"pour qu'ils durent."</em>
  const em51 = global.document.createElement('em');
  em51.setAttribute('data-waas', '51');
  em51.appendChild(makeWord('pour'));
  em51.appendChild(global.document.createTextNode(' '));
  em51.appendChild(makeWord("qu'ils"));
  em51.appendChild(global.document.createTextNode(' '));
  em51.appendChild(makeWord('durent.'));
  p.appendChild(em51);

  global.document.body.appendChild(p);

  const map = {
    slots: [
      { n: 49, k: '/html/body/main/section[2]/div/p[2]', t: 'text', p: 't0', g: 'p',
        x: 'Un site, une API, une automatisation : la forme change,' },
      { n: 50, k: '/html/body/main/section[2]/div/p[2]/em[1]', t: 'text', p: 't0', g: 'em',
        x: "pas l'exigence." },
      { n: 49, k: '/html/body/main/section[2]/div/p[2]/text()[normalize-space()][2]', t: 'text', p: 't1', g: 'p',
        x: "On conçoit des outils rapides, solides et simples à utiliser, puis on reste à vos côtés" },
      { n: 51, k: '/html/body/main/section[2]/div/p[2]/em[2]', t: 'text', p: 't0', g: 'em',
        x: "pour qu'ils durent." }
    ]
  };

  buildSlots(map);

  const slot49_0 = global.state.byKey.get('/html/body/main/section[2]/div/p[2]');
  const slot50 = global.state.byKey.get('/html/body/main/section[2]/div/p[2]/em[1]');
  const slot49_1 = global.state.byKey.get('/html/body/main/section[2]/div/p[2]/text()[normalize-space()][2]');
  const slot51 = global.state.byKey.get('/html/body/main/section[2]/div/p[2]/em[2]');

  assert(slot49_0 && slot50 && slot49_1 && slot51, 'Tous les 4 slots doivent exister');

  assert.strictEqual(slot49_0.ok, true, 'slot p (t0) doit être guéri et ok');
  assert.strictEqual(slot50.ok, true, 'slot em 50 doit être guéri et ok');
  assert.strictEqual(slot49_1.ok, true, 'slot p (t1) doit être guéri et ok');
  assert.strictEqual(slot51.ok, true, 'slot em 51 doit être guéri et ok');

  // Vérifier la structure DOM et le contenu
  const nodeP0 = textNodeAt(p, 't0');
  assert.strictEqual(norm(nodeP0.nodeValue), 'Un site, une API, une automatisation : la forme change,');

  const nodeEm50 = textNodeAt(em50, 't0');
  assert.strictEqual(norm(nodeEm50.nodeValue), "pas l'exigence.");

  const nodeP1 = textNodeAt(p, 't1');
  assert.strictEqual(norm(nodeP1.nodeValue), "On conçoit des outils rapides, solides et simples à utiliser, puis on reste à vos côtés");

  const nodeEm51 = textNodeAt(em51, 't0');
  assert.strictEqual(norm(nodeEm51.nodeValue), "pour qu'ils durent.");

  // Test de writeValue sur le slot guéri
  writeValue(slot50, em50, "toujours l'excellence.");
  assert.strictEqual(norm(textNodeAt(em50, 't0').nodeValue), "toujours l'excellence.");
});

check("buildTextPolygon : géométrie polygonale exacte (rectangles simples, marches, lignes disjointes)", () => {
  // Rectangle simple
  const single = buildTextPolygon([{ left: 10, top: 10, right: 100, bottom: 30, width: 90, height: 20 }], 3);
  assert.strictEqual(single, 'M 7 7 H 103 V 33 H 7 Z');

  // Multi-ligne en marche d'escalier (texte qui s'arrête avant un <em> sur la ligne 2)
  const stepped = buildTextPolygon([
    { left: 100, top: 0, right: 500, bottom: 30, width: 400, height: 30 },
    { left: 100, top: 30, right: 250, bottom: 60, width: 150, height: 30 }
  ], 3);
  assert.strictEqual(stepped, 'M 97 -3 L 503 -3 L 503 30 L 253 30 L 253 63 L 97 63 L 97 30 L 97 -3 Z');

  // Lignes disjointes sans recouvrement horizontal
  const disjoint = buildTextPolygon([
    { left: 10, top: 10, right: 30, bottom: 30, width: 20, height: 20 },
    { left: 70, top: 50, right: 100, bottom: 70, width: 30, height: 20 }
  ], 3);
  assert.strictEqual(disjoint, 'M 7 7 H 33 V 33 H 7 Z M 67 47 H 103 V 73 H 67 Z');
});

check("Contour polygonal sans chevauchement : paragraphe avec <em> imbriqué (cas réel 49 / 50)", () => {
  // Le paragraphe data-waas="49" avec em 50
  const p = global.state.byNumber.get(49)[0].el;
  const em50 = global.state.byNumber.get(50)[0].el;
  const slot49_0 = global.state.byKey.get('/html/body/main/section[2]/div/p[2]');
  const slot50 = global.state.byKey.get('/html/body/main/section[2]/div/p[2]/em[1]');

  const nodeP0 = textNodeAt(p, 't0');
  const nodeEm50 = textNodeAt(em50, 't0');

  // Ligne 1 : "Un site, une API..." (largeur 500)
  // Ligne 2 : "la forme change, " (de 100 à 350)
  nodeP0._rects = [
    { left: 100, top: 100, right: 600, bottom: 130, width: 500, height: 30 },
    { left: 100, top: 130, right: 350, bottom: 160, width: 250, height: 30 }
  ];
  // <em> "pas l'exigence." sur la ligne 2 commence à 360 et va jusqu'à 500
  nodeEm50._rects = [
    { left: 360, top: 130, right: 500, bottom: 160, width: 140, height: 30 }
  ];
  em50._box = { left: 360, top: 130, right: 500, bottom: 160, width: 140, height: 30 };

  const rects49 = rectsOf({ slot: slot49_0, el: p });
  assert.strictEqual(rects49.length, 2, 'slot 49 doit avoir exactement ses 2 rects de texte');

  const poly49 = buildTextPolygon(rects49, 2);
  // Vérifier que sur la ligne 2 (y=130..160), le polygone de 49 ne dépasse jamais x=352
  assert(poly49.includes('L 352 130') || poly49.includes('L 352'), 'Le contour de 49 doit s’arrêter avant l’em à 352');
  assert(!poly49.includes('500 160'), 'Le contour de 49 ne doit jamais s’étendre jusqu’à l’em');

  // Vérifier le rect de l'em 50
  const rects50 = rectsOf({ slot: slot50, el: em50 });
  assert.strictEqual(rects50.length, 1);
  assert.strictEqual(rects50[0].left, 360);
  assert.strictEqual(rects50[0].right, 500);
});

check("Élimination de la divergence avant/après double-clic : <h2 data-waas=\"53\"> avec <em data-waas=\"54\">", () => {
  // Création de <h2 data-waas="53">Cinq métiers,<br><em data-waas="54">un seul interlocuteur.</em></h2>
  const h2 = global.document.createElement('h2');
  h2.setAttribute('data-waas', '53');
  const t0 = global.document.createTextNode('Cinq métiers,');
  const br = global.document.createElement('br');
  const em54 = global.document.createElement('em');
  em54.setAttribute('data-waas', '54');
  const tEm = global.document.createTextNode('un seul interlocuteur.');
  em54.appendChild(tEm);
  h2.appendChild(t0);
  h2.appendChild(br);
  h2.appendChild(em54);
  global.document.body.appendChild(h2);

  // Positionnement géométrique :
  // Ligne 1 : "Cinq métiers," (left: 50, top: 200, right: 250, bottom: 240)
  t0._rects = [{ left: 50, top: 200, right: 250, bottom: 240, width: 200, height: 40 }];
  // Ligne 2 : "un seul interlocuteur." dans em54 (left: 50, top: 245, right: 380, bottom: 285)
  tEm._rects = [{ left: 50, top: 245, right: 380, bottom: 285, width: 330, height: 40 }];
  em54._box = { left: 50, top: 245, right: 380, bottom: 285, width: 330, height: 40 };
  // Boîte globale du <h2> englobant les 2 lignes
  h2._box = { left: 50, top: 200, right: 380, bottom: 285, width: 330, height: 85 };

  const slot53 = {
    key: '/html/body/h2', kind: 'text', pos: 't0', tag: 'h2',
    number: 53, el: h2, ok: true, label: 'Titre'
  };
  const slot54 = {
    key: '/html/body/h2/em', kind: 'text', pos: 't0', tag: 'em',
    number: 54, el: em54, ok: true, label: 'Italique'
  };

  global.state.slots.push(slot53, slot54);
  global.state.byKey.set(slot53.key, slot53);
  global.state.byKey.set(slot54.key, slot54);
  global.state.byNumber.set(53, [slot53]);
  global.state.byNumber.set(54, [slot54]);

  const hit53 = { slot: slot53, el: h2 };

  // 1. AVANT double-clic (sélection / survol) :
  // Doit retourner UNIQUEMENT la ligne 1 ("Cinq métiers,"), et NON la boîte globale de h2 !
  const rectsBefore = rectsOf(hit53);
  assert.strictEqual(rectsBefore.length, 1);
  assert.strictEqual(rectsBefore[0].left, 50);
  assert.strictEqual(rectsBefore[0].right, 250);
  assert.strictEqual(rectsBefore[0].bottom, 240, 'Le survol/sélection ne doit pas englober la ligne 2 !');

  // 2. APRÈS double-clic (mode édition) :
  const editHost = global.document.createElement('waas-edit');
  editHost._rects = [{ left: 50, top: 200, right: 250, bottom: 240, width: 200, height: 40 }];
  global.state.editing = { slot: slot53, hit: hit53, host: editHost, el: h2 };

  const rectsAfter = rectsOf(hit53);
  assert.strictEqual(rectsAfter.length, 1);
  assert.strictEqual(rectsAfter[0].left, 50);
  assert.strictEqual(rectsAfter[0].right, 250);
  assert.strictEqual(rectsAfter[0].bottom, 240);

  // Vérifier la parfaite concordance avant et après édition (zéro divergence)
  assert.deepStrictEqual(rectsBefore[0], rectsAfter[0], 'La zone avant double-clic doit être IDENTIQUE à celle après double-clic');
  global.state.editing = null;
});

check("Interception des liens en mode édition : un clic dans l'éditeur ne doit jamais rediriger", () => {
  // Création d'un lien <a href="/contact">Contactez-nous</a>
  const a = global.document.createElement('a');
  a.setAttribute('href', '/contact');
  const waasEdit = global.document.createElement('waas-edit');
  const text = global.document.createTextNode('Contactez-nous');
  waasEdit.appendChild(text);
  a.appendChild(waasEdit);
  global.document.body.appendChild(a);

  const slotLink = {
    key: '/html/body/a', kind: 'text', pos: 't0', tag: 'a',
    number: 99, el: a, ok: true, label: 'Lien'
  };
  const hitLink = { slot: slotLink, el: a };

  // Activer le mode édition sur ce lien
  global.state.active = true;
  global.state.mode = 'edit';
  global.state.editing = { slot: slotLink, hit: hitLink, host: waasEdit, el: a };

  // 1. linkOf retrouve bien la balise <a>
  const foundLink = linkOf(text);
  assert.strictEqual(foundLink, a, 'linkOf doit identifier la balise <a> parente du texte');

  // 2. Clic normal dans l'éditeur (pour placer le curseur, déplacer, etc.)
  let prevented = false;
  let stopped = false;
  let navigated = false;
  global.window.location.assign = () => { navigated = true; };

  const clickEvent = {
    button: 0,
    target: text,
    ctrlKey: false,
    metaKey: false,
    preventDefault() { prevented = true; },
    stopPropagation() { stopped = true; },
    stopImmediatePropagation() { stopped = true; }
  };

  onClick(clickEvent);

  assert.strictEqual(prevented, true, 'Le clic sur un lien dans l’éditeur DOIT appeler preventDefault()');
  assert.strictEqual(stopped, true, 'Le clic sur un lien dans l’éditeur DOIT stopper la propagation');
  assert.strictEqual(navigated, false, 'Le clic dans l’éditeur ne doit JAMAIS déclencher de redirection');

  global.state.editing = null;
});

if (failures.length) {
  console.error(`\n${failures.length} échec(s) dans dom_healing.test.js`);
  process.exit(1);
} else {
  console.log(`\n${passed}/${passed} vérifications réussies dans dom_healing.test.js\n`);
}
