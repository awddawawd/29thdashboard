/*
 * Pont d'édition · 3/3 — comportement : sélection, édition en place, verrouillage des
 * interactions du site, protocole avec le Studio, démarrage.
 *
 * Protocole postMessage (origine verrouillée sur le Studio dès WAAS_INIT) :
 *   pont → Studio  WAAS_BRIDGE_READY · WAAS_SELECTED · WAAS_EDIT_START · WAAS_EDIT_END
 *                  WAAS_CHANGE · WAAS_IMAGE_REQUEST · WAAS_PANEL_REQUEST · WAAS_SHORTCUT
 *                  WAAS_SCROLL · WAAS_NAVIGATING · WAAS_COMMITTED
 *   Studio → pont  WAAS_INIT · WAAS_SET_MODE · WAAS_SET_OPTIONS · WAAS_APPLY
 *                  WAAS_REVEAL · WAAS_COMMIT
 * 29th studio · © 2026
 */

const PROTOCOL = 2;
const READY_RETRY_MS = 800;
const READY_ATTEMPTS = 15;
const SCROLL_REPORT_MS = 250;

const script = document.currentScript;
const FILE = (script && script.getAttribute('data-waas-file')) || '';
const MAP_URL = (script && script.getAttribute('data-waas-map')) || '';

/** Hôtes où l'édition directe échoue (Firefox) : le texte est enveloppé. */
const WRAP_HOSTS = new Set(['BUTTON', 'SUMMARY']);
const FORM_CONTROLS = 'input, select, textarea, button, option, [contenteditable="true"]';

const state = {
  active: false, parentOrigin: null, mode: 'edit', focusStyle: 'subtle',
  slots: [], byKey: new Map(), byNumber: new Map(),
  hover: null, selected: null, editing: null, pointer: null
};

function applyChanges(changes) {
  if (!Array.isArray(changes)) return;
  changes.forEach((change) => {
    const slot = change && state.byKey.get(change.key);
    if (!slot || typeof change.value !== 'string' || change.value.length > MAX_VALUE) return;
    const targets = instances(slot);
    if (state.editing && state.editing.slot === slot) finishEditing(false);
    // Même élément que l'édition en cours : l'enveloppe décalerait les positions des textes.
    else if (state.editing && targets.includes(state.editing.el)) finishEditing(true);
    targets.forEach((el) => writeValue(slot, el, change.value));
  });
  schedule();
}

/* ----------------------------------------------------------------------
   Sélection et navigation au clavier
   ---------------------------------------------------------------------- */
function select(hit, options) {
  const opts = options || {};
  state.selected = hit || null;
  if (hit && opts.flash) {
    ui.select.classList.remove('is-flash');
    void (ui.select.offsetWidth || (ui.select.getBoundingClientRect && ui.select.getBoundingClientRect()));
    ui.select.classList.add('is-flash');
  }
  if (hit) announce(`${hit.slot.label} sélectionné.`);
  schedule();
  if (!opts.silent) post('WAAS_SELECTED', { key: hit ? hit.slot.key : null });
}

function visible(slot) {
  const el = primary(slot);
  if (!el) return false;
  if (slot.kind === 'text') {
    const node = textNodeAt(el, slot.pos);
    return Boolean(node) && textRects(node).length > 0;
  }
  return slot.kind === 'image' && el.getClientRects().length > 0;
}

function neighbour(from, direction, textOnly) {
  const candidates = state.slots.filter((slot) => slot.ok && (slot.kind === 'text' || (!textOnly && slot.kind === 'image')) && visible(slot));
  if (!candidates.length) return null;
  const index = from ? candidates.indexOf(from.slot) : -1;
  const next = candidates[(index + direction + candidates.length) % candidates.length];
  return { slot: next, el: primary(next) };
}

function reveal(key, flash) {
  const slot = state.byKey.get(key);
  const el = slot && primary(slot);
  if (!el) return;
  const behavior = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
  el.scrollIntoView({ block: 'center', inline: 'nearest', behavior });
  select({ slot, el }, { silent: true, flash });
}

/* ----------------------------------------------------------------------
   Édition en place
   ---------------------------------------------------------------------- */
function makeEditable(host) {
  try {
    host.contentEditable = 'plaintext-only';
  } catch (error) {
    host.contentEditable = 'true';
  }
  if (host.contentEditable !== 'plaintext-only') host.contentEditable = 'true';
}

function placeCaret(host, caret) {
  const selection = window.getSelection();
  const range = document.createRange();
  if (caret && caret.node && host.contains(caret.node)) {
    range.setStart(caret.node, Math.min(caret.offset, caret.node.nodeType === 3 ? caret.node.length : 0));
  } else {
    range.selectNodeContents(host);
    range.collapse(false);
  }
  range.collapse(true);
  selection.removeAllRanges();
  selection.addRange(range);
}

function startEditing(hit, point) {
  // Session précédente close d'abord : elle peut envelopper un nœud du même élément.
  finishEditing(true);
  const { slot, el } = hit;
  const node = textNodeAt(el, slot.pos);
  if (!node || slot.kind !== 'text' || !slot.ok) return;
  // Point d'insertion calculé avant toute modification du DOM : pas de mot sélectionné.
  const caret = point ? caretFromPoint(point.x, point.y) : null;
  const simple = Array.from(el.childNodes).every((child) => child.nodeType === 3) && !WRAP_HOSTS.has(el.tagName);
  let host = el;
  if (!simple) {
    host = document.createElement('waas-edit');
    node.parentNode.insertBefore(host, node);
    host.appendChild(node);
  }
  state.editing = {
    hit, slot, el, host, simple, original: node.nodeValue,
    before: slot.pre ? node.nodeValue : norm(node.nodeValue),
    outline: host.style.outline, editable: host.getAttribute('contenteditable'),
    spellcheck: host.getAttribute('spellcheck')
  };
  makeEditable(host);
  host.setAttribute('spellcheck', 'true');
  host.style.outline = 'none';
  host.addEventListener('beforeinput', onBeforeInput);
  host.addEventListener('paste', onPaste);
  host.addEventListener('drop', swallow);
  host.addEventListener('input', schedule);
  host.focus({ preventScroll: true });
  placeCaret(host, caret);
  state.hover = null;
  updateCursor();
  schedule();
  post('WAAS_EDIT_START', { key: slot.key });
  announce(`Modification : ${slot.label}. Entrée pour valider, Échap pour annuler.`);
}

function readHostText(host, pre) {
  const clone = host.cloneNode(true);
  clone.querySelectorAll('br').forEach((br) => br.replaceWith('\n'));
  const text = clone.textContent;
  return pre ? text : norm(text.replace(/\n/g, ' '));
}

function finishEditing(commit) {
  const session = state.editing;
  if (!session) return;
  state.editing = null;
  const { slot, host, simple, original } = session;
  host.removeEventListener('beforeinput', onBeforeInput);
  host.removeEventListener('paste', onPaste);
  host.removeEventListener('drop', swallow);
  host.removeEventListener('input', schedule);
  const typed = commit ? readHostText(host, slot.pre).slice(0, MAX_VALUE) : null;
  if (session.editable == null) host.removeAttribute('contenteditable');
  else host.setAttribute('contenteditable', session.editable);
  if (session.spellcheck == null) host.removeAttribute('spellcheck');
  else host.setAttribute('spellcheck', session.spellcheck);
  host.style.outline = session.outline;
  const text = typed == null ? original : (slot.pre ? typed : lead(original) + typed + trail(original));
  // Toujours un nœud texte, même vide : un texte effacé reste rétablissable (annuler).
  if (simple) host.replaceChildren(document.createTextNode(text));
  else host.replaceWith(document.createTextNode(text));
  const selection = window.getSelection();
  if (selection) selection.removeAllRanges();
  post('WAAS_EDIT_END', { key: slot.key, committed: commit });
  if (typed != null && typed !== session.before) {
    instances(slot).forEach((el) => { if (el !== session.el) writeValue(slot, el, typed); });
    post('WAAS_CHANGE', { key: slot.key, value: typed });
    announce(`${slot.label} modifié. Pensez à publier.`);
  }
  state.selected = { slot, el: session.el };
  updateCursor();
  schedule();
}

function insertText(text) {
  if (document.execCommand && document.execCommand('insertText', false, text)) return;
  const selection = window.getSelection();
  if (!selection || !selection.rangeCount) return;
  const range = selection.getRangeAt(0);
  range.deleteContents();
  const node = document.createTextNode(text);
  range.insertNode(node);
  range.setStartAfter(node);
  range.collapse(true);
  selection.removeAllRanges();
  selection.addRange(range);
}

function selectedLength() {
  const selection = window.getSelection();
  return selection ? selection.toString().length : 0;
}

function room() {
  return state.editing ? MAX_VALUE - (state.editing.host.textContent.length - selectedLength()) : 0;
}

function onBeforeInput(event) {
  const type = event.inputType || '';
  if (type.indexOf('format') === 0 || type === 'insertFromDrop' || type === 'deleteByDrag') {
    event.preventDefault();
  } else if (type === 'insertParagraph' || type === 'insertLineBreak') {
    event.preventDefault();
    if (state.editing && state.editing.slot.pre) insertText('\n');
    else finishEditing(true);
  } else if ((type === 'insertText' || type === 'insertReplacementText') && event.data && event.data.length > room()) {
    event.preventDefault();
    announce(`Limite de ${MAX_VALUE} caractères atteinte.`);
  }
}

function onPaste(event) {
  event.preventDefault();
  if (!state.editing) return;
  const data = event.clipboardData ? event.clipboardData.getData('text/plain') : '';
  let text = state.editing.slot.pre ? data.replace(/\r\n?/g, '\n') : data.replace(/[\r\n\t]+/g, ' ');
  const available = room();
  if (text.length > available) {
    text = text.slice(0, Math.max(0, available));
    announce(`Limite de ${MAX_VALUE} caractères atteinte : texte collé tronqué.`);
  }
  if (text) insertText(text);
}

function handleEditingKey(event) {
  const session = state.editing;
  const mod = event.ctrlKey || event.metaKey;
  const key = event.key;
  if (key === 'Escape') {
    swallow(event);
    finishEditing(false);
  } else if (key === 'Enter' && !event.isComposing) {
    swallow(event);
    if (session.slot.pre && !mod) insertText('\n');
    else finishEditing(true);
  } else if (key === 'Tab') {
    swallow(event);
    const from = session.hit;
    finishEditing(true);
    const next = neighbour(from, event.shiftKey ? -1 : 1, true);
    if (next) {
      next.el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
      select(next);
      startEditing(next, null);
    }
  } else if (mod && /^[biu]$/i.test(key)) {
    swallow(event);
  } else if (mod && key.toLowerCase() === 's') {
    swallow(event);
    finishEditing(true);
    post('WAAS_SHORTCUT', { action: 'save' });
  } else if (key === ' ' && session.host.closest('button, summary, a, label')) {
    swallow(event);
    insertText(' ');
  } else {
    event.stopPropagation();                // la saisie ne déclenche aucun raccourci du site
  }
}

function requestImage(hit) {
  const { slot, el } = hit;
  const alt = slotsOf(el).find((item) => item.kind === 'attr' && item.pos === '@alt');
  const rect = el.getBoundingClientRect();
  post('WAAS_IMAGE_REQUEST', {
    key: slot.key, src: el.currentSrc || el.getAttribute('src') || '', altKey: alt ? alt.key : null,
    alt: el.getAttribute('alt') || '', rendered: { width: Math.round(rect.width), height: Math.round(rect.height) },
    natural: { width: el.naturalWidth || 0, height: el.naturalHeight || 0 }, dpr: window.devicePixelRatio || 1
  });
}

function activate(hit, point) {
  if (!hit) return;
  select(hit);
  if (hit.slot.kind === 'text' && hit.slot.ok) startEditing(hit, point);
  else if (hit.slot.kind === 'image') requestImage(hit);
  else post('WAAS_PANEL_REQUEST', { key: hit.slot.key });
}

/* ----------------------------------------------------------------------
   Verrouillage des interactions du site en mode édition
   ---------------------------------------------------------------------- */
function swallow(event) {
  event.preventDefault();
  event.stopImmediatePropagation();
}

const editActive = () => state.active && state.mode === 'edit';
const inEditor = (target) => Boolean(state.editing && target instanceof Node && state.editing.host.contains(target));

function editUrl(href) {
  let url;
  try {
    url = new URL(href, window.location.href);
  } catch (error) {
    return null;
  }
  if (!/^https?:$/.test(url.protocol)) return null;
  if (url.origin !== window.location.origin) return url;
  url.searchParams.set('waas_edit', 'true');
  url.searchParams.set('waas_t', String(Date.now()));
  return url;
}

function followLink(link) {
  const url = editUrl(link.getAttribute('href') || '');
  if (!url) return;
  if (url.origin !== window.location.origin) {
    window.open(url.toString(), '_blank', 'noopener');
    return;
  }
  if (url.pathname === window.location.pathname && url.hash) {
    const anchor = document.getElementById(decodeURIComponent(url.hash.slice(1)));
    if (anchor) anchor.scrollIntoView({ block: 'start' });
    return;
  }
  post('WAAS_NAVIGATING', { href: url.pathname });
  window.location.assign(url.toString());
}

function linkOf(target) {
  const el = target instanceof Element ? target : (target && target.parentElement instanceof Element ? target.parentElement : null);
  if (el) {
    const link = el.closest('a[href]');
    if (link) return link;
  }
  if (state.editing) {
    if (state.editing.host instanceof Element) {
      const link = state.editing.host.closest('a[href]');
      if (link) return link;
    }
    if (state.editing.el instanceof Element) {
      const link = state.editing.el.closest('a[href]');
      if (link) return link;
    }
  }
  return null;
}

function onClick(event) {
  if (!state.active) return;
  const link = linkOf(event.target);
  if (state.mode === 'preview') {
    // L'aperçu reste dans le Studio : les liens internes gardent l'éditeur, les externes s'ouvrent à part.
    const plain = event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey;
    if (link && plain && !link.hasAttribute('download') && (link.getAttribute('target') || '_self') === '_self') {
      event.preventDefault();
      followLink(link);
    }
    return;
  }
  if (inEditor(event.target)) {
    if (link) {
      event.preventDefault();
      event.stopPropagation();
      if (event.ctrlKey || event.metaKey) {
        finishEditing(true);
        followLink(link);
      }
    }
    return;
  }
  swallow(event);
  if (event.button !== 0) return;
  if (link && (event.ctrlKey || event.metaKey)) {
    followLink(link);
    return;
  }
  select(hitAt(event.clientX, event.clientY));
}

function onDoubleClick(event) {
  if (!editActive() || inEditor(event.target)) return;
  swallow(event);
  activate(hitAt(event.clientX, event.clientY), { x: event.clientX, y: event.clientY });
}

function onMouseDown(event) {
  if (!editActive() || inEditor(event.target)) return;
  if (state.editing) {
    finishEditing(true);
    event.preventDefault();
  }
  // Le double-clic ne sélectionne plus de mot ; aucun champ du site ne prend le focus.
  if (event.detail > 1 || (event.target instanceof Element && event.target.closest(FORM_CONTROLS))) event.preventDefault();
  event.stopImmediatePropagation();       // carrousels, glisser-déposer, menus du site
}

function onPointerGuard(event) {
  if (editActive() && !inEditor(event.target)) event.stopImmediatePropagation();
}

function onBlocked(event) {
  if (!editActive()) return;
  if (inEditor(event.target)) {
    if (event.type === 'auxclick' && linkOf(event.target)) swallow(event);
    if (event.type === 'submit') swallow(event);
    return;
  }
  swallow(event);
}

function onPointerMove(event) {
  if (!editActive() || state.editing) return;
  state.pointer = { x: event.clientX, y: event.clientY };
  if (!hoverFrame) hoverFrame = window.requestAnimationFrame(updateHover);
}

let hoverFrame = 0;
function updateHover() {
  hoverFrame = 0;
  if (!editActive() || state.editing || !state.pointer) return;
  const hit = hitAt(state.pointer.x, state.pointer.y);
  if (sameHit(hit, state.hover)) return;
  state.hover = hit;
  updateCursor();
  schedule();
}

function onPointerLeave(event) {
  if (event.relatedTarget) return;
  state.pointer = null;
  state.hover = null;
  updateCursor();
  schedule();
}

function forward(event, action) {
  swallow(event);
  post('WAAS_SHORTCUT', { action });
}

function onKeyDown(event) {
  if (!state.active) return;
  if (state.editing) {
    handleEditingKey(event);
    return;
  }
  const mod = (event.ctrlKey || event.metaKey) && !event.altKey;
  const key = event.key.toLowerCase();
  if (mod && key === 's') return forward(event, 'save');
  if (mod && key === 'z' && !event.shiftKey) return forward(event, 'undo');
  if (mod && (key === 'y' || (key === 'z' && event.shiftKey))) return forward(event, 'redo');
  if (state.mode !== 'edit' || !state.selected) return;
  if (event.key === 'Escape') {
    swallow(event);
    select(null);
  } else if (event.key === 'Enter') {
    swallow(event);
    activate(state.selected, null);
  } else if (event.key === 'Tab') {
    swallow(event);
    const next = neighbour(state.selected, event.shiftKey ? -1 : 1, false);
    if (next) {
      next.el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
      select(next);
    }
  }
}

let scrollTimer = 0;
function onScroll() {
  if (!state.active) return;
  if (state.pointer && !state.editing && !hoverFrame) hoverFrame = window.requestAnimationFrame(updateHover);
  schedule();
  if (!scrollTimer) {
    scrollTimer = window.setTimeout(() => {
      scrollTimer = 0;
      post('WAAS_SCROLL', { x: Math.round(window.scrollX), y: Math.round(window.scrollY) });
    }, SCROLL_REPORT_MS);
  }
}

function listen() {
  const capture = { capture: true };
  window.addEventListener('click', guard(onClick), capture);
  window.addEventListener('auxclick', guard(onBlocked), capture);
  window.addEventListener('dblclick', guard(onDoubleClick), capture);
  window.addEventListener('mousedown', guard(onMouseDown), capture);
  ['pointerdown', 'pointerup', 'mouseup', 'touchstart', 'touchend'].forEach((type) =>
    window.addEventListener(type, guard(onPointerGuard), { capture: true, passive: true }));
  ['submit', 'dragstart'].forEach((type) => window.addEventListener(type, guard(onBlocked), capture));
  window.addEventListener('keydown', guard(onKeyDown), capture);
  document.addEventListener('mousemove', guard(onPointerMove), { capture: true, passive: true });
  document.addEventListener('mouseout', guard(onPointerLeave), { capture: true, passive: true });
  document.addEventListener('scroll', guard(onScroll), { capture: true, passive: true });
  window.addEventListener('resize', guard(schedule));
  window.addEventListener('blur', guard(() => { if (state.editing) finishEditing(true); }));
  window.addEventListener('message', guard(onMessage));
}

/** Une erreur du pont ne doit jamais casser la page du client. */
function guard(handler) {
  return function guarded(event) {
    try {
      return handler(event);
    } catch (error) {
      if (window.console) window.console.warn('[WaaS Studio]', error);
      return undefined;
    }
  };
}

/* ----------------------------------------------------------------------
   Protocole avec le Studio
   ---------------------------------------------------------------------- */
function post(type, data) {
  if (!state.parentOrigin) return;
  window.parent.postMessage(Object.assign({ type, protocol: PROTOCOL }, data || {}), state.parentOrigin);
}

function setMode(mode) {
  const next = mode === 'preview' ? 'preview' : 'edit';
  if (next === 'preview' && state.editing) finishEditing(true);
  state.mode = next;
  state.hover = null;
  if (next === 'preview') state.selected = null;
  updateCursor();
  schedule();
}

function restoreScroll(scroll) {
  if (!scroll || !Number.isFinite(scroll.x) || !Number.isFinite(scroll.y)) return;
  const root = document.documentElement;
  const previous = root.style.scrollBehavior;
  root.style.scrollBehavior = 'auto';
  window.scrollTo(scroll.x, scroll.y);
  root.style.scrollBehavior = previous;
}

function onMessage(event) {
  if (event.source !== window.parent) return;
  const data = event.data;
  if (!data || typeof data !== 'object' || typeof data.type !== 'string') return;
  if (!state.parentOrigin) {
    if (data.type !== 'WAAS_INIT') return;
    state.parentOrigin = event.origin;      // le Studio : seule origine admise à encadrer la vitrine
  } else if (event.origin !== state.parentOrigin) {
    return;
  }
  switch (data.type) {
    case 'WAAS_INIT':
      state.active = true;
      state.focusStyle = data.focusStyle === 'strong' ? 'strong' : 'subtle';
      setMode(data.mode);
      applyChanges(data.values);
      restoreScroll(data.scroll);
      if (typeof data.reveal === 'string') reveal(data.reveal, true);
      break;
    case 'WAAS_SET_MODE':
      setMode(data.mode);
      break;
    case 'WAAS_SET_OPTIONS':
      state.focusStyle = data.focusStyle === 'strong' ? 'strong' : 'subtle';
      schedule();
      break;
    case 'WAAS_APPLY':
      applyChanges(data.changes);
      break;
    case 'WAAS_REVEAL':
      if (typeof data.key === 'string') reveal(data.key, true);
      break;
    case 'WAAS_COMMIT':
      finishEditing(true);
      post('WAAS_COMMITTED', { requestId: data.requestId });
      break;
    default:
      break;
  }
}

function announceReady() {
  let attempts = 0;
  const send = () => {
    if (state.parentOrigin || attempts >= READY_ATTEMPTS) return;
    attempts += 1;
    // Avant WAAS_INIT, l'origine du Studio est inconnue : le message ne contient que des
    // informations publiques (page, clés des éléments).
    window.parent.postMessage({
      type: 'WAAS_BRIDGE_READY', protocol: PROTOCOL, file: FILE, path: window.location.pathname,
      title: document.title, scroll: { x: Math.round(window.scrollX), y: Math.round(window.scrollY) },
      slots: state.slots.map((slot) => ({ key: slot.key, kind: slot.kind, ok: slot.ok }))
    }, '*');
    window.setTimeout(send, READY_RETRY_MS);
  };
  send();
}

async function boot() {
  if (window.top === window.self) return;
  if (document.readyState === 'loading') {
    await new Promise((resolve) => document.addEventListener('DOMContentLoaded', resolve, { once: true }));
  }
  let map = null;
  try {
    const response = await fetch(MAP_URL, { credentials: 'same-origin', cache: 'no-cache' });
    map = response.ok ? await response.json() : null;
  } catch (error) {
    map = null;
  }
  buildSlots(map);
  mountOverlay();
  listen();
  announceReady();
}

boot().catch((error) => {
  if (window.console) window.console.warn('[WaaS Studio] Pont indisponible :', error);
});
