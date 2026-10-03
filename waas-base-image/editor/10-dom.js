/*
 * Pont d'édition · 1/3 — DOM : valeurs, emplacements de la page, repérage sous le pointeur.
 * Assemblé avec 20-overlay.js et 30-bridge.js dans une même portée par
 * sync_engine.install_bridge_asset() (un seul fichier servi, sans étape de build).
 * Fournit : norm, lead, trail, safeUrl, labelOf, textNodeAt, instances, slotsOf,
 * primary, isSplitHost, healSplittedElement, buildSlots, writeValue, caretFromPoint,
 * textRects, hitAt, sameHit.
 * 29th studio · © 2026
 */

const MAX_VALUE = 5000;
const HUGE_AREA_RATIO = 0.4;
const TAG_LABELS = {
  h1: 'Titre principal', h2: 'Titre de section', h3: 'Sous-titre', h4: 'Intertitre', h5: 'Intertitre',
  h6: 'Intertitre', p: 'Paragraphe', a: 'Lien', button: 'Bouton', li: 'Élément de liste', td: 'Cellule',
  th: 'En-tête de tableau', figcaption: 'Légende', blockquote: 'Citation', q: 'Citation', cite: 'Source',
  label: 'Libellé', strong: 'Texte en gras', b: 'Texte en gras', em: 'Texte en italique', i: 'Texte en italique',
  small: 'Petit texte', title: 'Titre de la page', summary: 'Résumé', dt: 'Terme', dd: 'Définition',
  address: 'Adresse', time: 'Date', legend: 'Légende du formulaire', caption: 'Légende du tableau',
  pre: 'Texte préformaté', code: 'Code', option: 'Option'
};
const ATTR_LABELS = {
  '@alt': 'Texte alternatif', '@placeholder': "Texte d'aide du champ", '@value': 'Bouton de formulaire',
  '@content': 'Description (Google)'
};

/* ----------------------------------------------------------------------
   Valeurs : mêmes règles que le moteur du conteneur
   ---------------------------------------------------------------------- */
const norm = (value) => String(value == null ? '' : value).replace(/[ \t\r\n]+/g, ' ').replace(/^ +| +$/g, '');
const lead = (text) => /^[ \t\r\n]*/.exec(text)[0];
const trail = (text) => /[ \t\r\n]*$/.exec(text)[0];

function safeUrl(value) {
  const url = String(value == null ? '' : value).trim();
  if (!url || url.length > 2048 || /[\s"<>`\\\u0000-\u001f\u007f]/.test(url)) return null;
  const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(url);
  return !scheme || /^https?$/i.test(scheme[1]) ? url : null;
}

function labelOf(slot) {
  if (slot.kind === 'image') return 'Image';
  if (slot.kind === 'attr') return ATTR_LABELS[slot.pos] || 'Attribut';
  const base = TAG_LABELS[slot.tag] || 'Texte';
  return /^h[1-6]$/.test(slot.tag) ? `${base} · ${slot.tag.toUpperCase()}` : base;
}

/* ----------------------------------------------------------------------
   Emplacements : carte de la page (numéros data-waas -> clés)
   ---------------------------------------------------------------------- */
function textNodeAt(el, pos) {
  const index = Number(String(pos).slice(1));
  if (!el || pos[0] !== 't' || !Number.isInteger(index)) return null;
  if (index === 0) {
    const first = el.firstChild;
    return first && first.nodeType === 3 ? first : null;
  }
  let count = 0;
  for (let child = el.firstChild; child; child = child.nextSibling) {
    if (child.nodeType === 3) continue;
    count += 1;
    if (count === index) {
      const next = child.nextSibling;
      return next && next.nodeType === 3 ? next : null;
    }
  }
  return null;
}

const instances = (slot) => Array.from(document.querySelectorAll(`[data-waas="${slot.number}"]`));
const slotsOf = (el) => (el && el.getAttribute ? state.byNumber.get(Number(el.getAttribute('data-waas'))) : null) || [];

function primary(slot) {
  if (!slot.el || !slot.el.isConnected) slot.el = instances(slot)[0] || null;
  return slot.el;
}

function isSplitHost(el) {
  if (!el || el.nodeType !== 1) return false;
  if (el.hasAttribute('data-split') || (el.classList && el.classList.contains('is-splitting'))) return true;
  if (el.closest && el.closest('[data-split], .is-splitting, [data-splittype]')) return true;
  return Boolean(el.querySelector && el.querySelector('.split-w, .split-char, .split-line, .word, .char, [data-split], [data-splittype]'));
}

/** Rétablit les nœuds texte découpés par une bibliothèque d'animation (Splitting.js, SplitType...). */
function healSplittedElement(el) {
  if (!el || el.nodeType !== 1) return;

  const isSplitWrapper = (node) => {
    if (!node || node.nodeType !== 1) return false;
    // Tout élément porteur d'un slot WaaS ou explicitement exclu doit rester intact
    if (node.hasAttribute('data-waas') || node.hasAttribute('data-waas-id') || node.hasAttribute('data-waas-skip')) {
      return false;
    }
    const tag = node.tagName.toLowerCase();
    // Les balises sémantiques ou interactives ne sont jamais des enveloppes artificielles de split
    if (tag !== 'span' && tag !== 'div') return false;
    const cls = typeof node.className === 'string' ? node.className : '';
    if (/\b(split-w|split-w__in|split-char|split-line|word|char|line)\b/.test(cls)) return true;
    if (node.hasAttribute('data-char') || node.hasAttribute('data-word') || node.hasAttribute('data-line')) return true;
    if (node.style && (node.style.getPropertyValue('--i') || node.style.getPropertyValue('--line') || node.style.getPropertyValue('--char') || node.style.getPropertyValue('--word'))) return true;
    if (el.hasAttribute('data-split') || (el.classList && el.classList.contains('is-splitting')) || (el.closest && el.closest('[data-split], .is-splitting'))) {
      return !node.id;
    }
    return false;
  };

  const candidates = Array.from(el.querySelectorAll('span, div')).filter(isSplitWrapper);
  if (!candidates.length) return;

  // On déplie en partant des feuilles vers la racine (ordre inverse)
  candidates.reverse().forEach((wrapper) => {
    const parent = wrapper.parentNode;
    if (!parent) return;
    while (wrapper.firstChild) {
      parent.insertBefore(wrapper.firstChild, wrapper);
    }
    parent.removeChild(wrapper);
  });

  el.normalize();
  if (el.classList) el.classList.remove('is-splitting');
}

function buildSlots(map) {
  const entries = map && Array.isArray(map.slots) ? map.slots : [];
  entries.forEach((entry) => {
    if (!entry || typeof entry.k !== 'string' || !Number.isInteger(entry.n)) return;
    const slot = {
      key: entry.k, kind: String(entry.t || 'text'), pos: String(entry.p || ''), tag: String(entry.g || ''),
      pre: Boolean(entry.pre), number: entry.n, el: null, ok: false
    };
    slot.label = labelOf(slot);
    const el = primary(slot);
    if (slot.kind === 'text') {
      let node = textNodeAt(el, slot.pos);
      if ((!node || norm(node.nodeValue) !== norm(entry.x)) && isSplitHost(el)) {
        instances(slot).forEach((target) => {
          const host = (target.closest && target.closest('[data-split], .is-splitting')) || target;
          healSplittedElement(host);
        });
        node = textNodeAt(el, slot.pos);
      }
      // Texte réécrit par un script du site (traduction, compteur…) : modifiable depuis le panneau seulement.
      slot.ok = Boolean(node) && norm(node.nodeValue) === norm(entry.x);
    } else {
      slot.ok = Boolean(el) && (slot.kind !== 'image' || el.tagName === 'IMG');
    }
    state.slots.push(slot);
    state.byKey.set(slot.key, slot);
    if (!state.byNumber.has(slot.number)) state.byNumber.set(slot.number, []);
    state.byNumber.get(slot.number).push(slot);
  });
}

/** Nœud texte à sa position, créé s'il manque (texte vidé côté serveur, puis rétabli). */
function ensureTextNode(el, pos) {
  const existing = textNodeAt(el, pos);
  if (existing) return existing;
  const index = Number(String(pos).slice(1));
  if (!Number.isInteger(index)) return null;
  const node = document.createTextNode('');
  if (index === 0) {
    el.insertBefore(node, el.firstChild);
    return node;
  }
  let count = 0;
  for (let child = el.firstChild; child; child = child.nextSibling) {
    if (child.nodeType === 3) continue;
    count += 1;
    if (count === index) {
      el.insertBefore(node, child.nextSibling);
      return node;
    }
  }
  return null;
}

function writeValue(slot, el, value) {
  if (slot.kind === 'text') {
    const node = ensureTextNode(el, slot.pos);
    if (node) node.nodeValue = slot.pre ? value : lead(node.nodeValue) + norm(value) + trail(node.nodeValue);
  } else if (slot.kind === 'image') {
    const url = safeUrl(value);
    if (!url || el.tagName !== 'IMG') return;
    el.setAttribute('src', url);
    if (el.hasAttribute('data-src')) el.setAttribute('data-src', url);
    ['srcset', 'sizes', 'data-srcset', 'data-sizes'].forEach((name) => el.removeAttribute(name));
    const picture = el.closest('picture');
    if (picture) {
      picture.querySelectorAll('source').forEach((source) => {
        source.setAttribute('srcset', url.replace(/,/g, '%2C'));
        source.removeAttribute('type');
        source.removeAttribute('sizes');
      });
    }
  } else if (slot.kind === 'attr') {
    el.setAttribute(slot.pos.slice(1), norm(value));
  }
}

/* ----------------------------------------------------------------------
   Repérage sous le pointeur (y compris sous un calque décoratif)
   ---------------------------------------------------------------------- */
function caretFromPoint(x, y) {
  if (document.caretPositionFromPoint) {
    const position = document.caretPositionFromPoint(x, y);
    return position && position.offsetNode ? { node: position.offsetNode, offset: position.offset } : null;
  }
  if (document.caretRangeFromPoint) {
    const range = document.caretRangeFromPoint(x, y);
    return range ? { node: range.startContainer, offset: range.startOffset } : null;
  }
  return null;
}

function textRects(node) {
  const range = document.createRange();
  range.selectNodeContents(node);
  return Array.from(range.getClientRects()).filter((rect) => rect.width > 0 && rect.height > 0);
}

const within = (rects, x, y, pad) => rects.some((rect) =>
  x >= rect.left - pad && x <= rect.right + pad && y >= rect.top - pad && y <= rect.bottom + pad);

function distance(rects, x, y) {
  return rects.reduce((best, rect) => {
    const dx = Math.max(rect.left - x, 0, x - rect.right);
    const dy = Math.max(rect.top - y, 0, y - rect.bottom);
    return Math.min(best, Math.hypot(dx, dy));
  }, Infinity);
}

function hitInElement(el, x, y) {
  const slots = slotsOf(el);
  if (!slots.length) return null;
  if (el.tagName === 'IMG') {
    const image = slots.find((slot) => slot.kind === 'image');
    return image ? { slot: image, el } : null;
  }
  const texts = slots.filter((slot) => slot.kind === 'text' && slot.ok);
  for (const slot of texts) {
    const node = textNodeAt(el, slot.pos);
    if (node && within(textRects(node), x, y, 3)) return { slot, el };
  }
  const box = el.getBoundingClientRect();
  const compact = box.width * box.height < window.innerWidth * window.innerHeight * HUGE_AREA_RATIO;
  if (texts.length === 1 && compact) {
    if (el.querySelector && el.querySelector('[data-waas]')) {
      const node = textNodeAt(el, texts[0].pos);
      const rects = node ? textRects(node) : [];
      if (rects.length && distance(rects, x, y) > 25) return null;
    }
    return { slot: texts[0], el };   // marge d'un bouton, d'un titre…
  }
  const field = slots.find((slot) => slot.kind === 'attr' && slot.ok && /^(INPUT|TEXTAREA)$/.test(el.tagName));
  if (field) return { slot: field, el };
  if (texts.length > 1 && compact) {
    let best = null;
    let bestDistance = Infinity;
    texts.forEach((slot) => {
      const node = textNodeAt(el, slot.pos);
      const gap = node ? distance(textRects(node), x, y) : Infinity;
      if (gap < bestDistance) { best = slot; bestDistance = gap; }
    });
    return best ? { slot: best, el } : null;
  }
  return null;
}

function hitAt(x, y) {
  const caret = caretFromPoint(x, y);
  if (caret && caret.node && caret.node.nodeType === 3 && caret.node.parentElement) {
    const owner = caret.node.parentElement;
    const slot = slotsOf(owner).find((item) => item.kind === 'text' && item.ok && textNodeAt(owner, item.pos) === caret.node);
    if (slot && within(textRects(caret.node), x, y, 3)) return { slot, el: owner };
  }
  const layers = document.elementsFromPoint ? document.elementsFromPoint(x, y) : [];
  for (const element of layers) {
    if (element === document.documentElement || element === document.body) break;
    if (!element.hasAttribute('data-waas')) continue;
    const hit = hitInElement(element, x, y);
    if (hit) return hit;
  }
  return null;
}

const sameHit = (a, b) => Boolean(a && b && a.slot === b.slot && a.el === b.el);
