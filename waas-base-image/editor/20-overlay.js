/*
 * Pont d'édition · 2/3 — interface superposée : contours polygonaux, projecteur vectoriel,
 * étiquettes et curseurs, dans un Shadow DOM fermé (aucun style ne fuit vers le site ni n'en vient).
 * Lit l'état du pont (state) ; fournit mountOverlay, schedule, render, announce,
 * updateCursor, union, rectsOf, buildTextPolygon.
 * 29th studio · © 2026
 */

const SPOT_PADDING = 6;
const ACCENT = '#2555e6';
const SVG_NS = 'http://www.w3.org/2000/svg';

/* ----------------------------------------------------------------------
   Interface superposée (Shadow DOM fermé, hors de la mise en page du site)
   ---------------------------------------------------------------------- */
const ui = {};

const OVERLAY_CSS = `
  :host { all: initial; position: fixed; inset: 0; pointer-events: none; z-index: 2147483647; }
  .overlay-canvas { position: fixed; inset: 0; width: 100vw; height: 100vh; pointer-events: none; overflow: visible; }
  .spot-veil { fill: rgba(15, 22, 35, 0.38); opacity: 0; pointer-events: none;
    transition: opacity 180ms cubic-bezier(0.22, 1, 0.36, 1); }
  .spot-veil.is-on { opacity: 1; }
  .spot-veil.is-strong { fill: rgba(15, 22, 35, 0.62); }
  .poly { fill: none; opacity: 0; pointer-events: none; stroke-linejoin: round; stroke-linecap: round;
    transition: opacity 140ms cubic-bezier(0.22, 1, 0.36, 1); }
  .poly.is-on { opacity: 1; }
  .poly.hover {
    stroke: ${ACCENT};
    stroke-width: 1.5;
    stroke-dasharray: 4 3;
    fill: rgba(37, 85, 230, 0.05);
  }
  .poly.select {
    stroke: ${ACCENT};
    stroke-width: 2;
    fill: rgba(37, 85, 230, 0.04);
  }
  .poly.select.is-flash {
    animation: poly-flash 900ms cubic-bezier(0.22, 1, 0.36, 1);
  }
  .poly.spot {
    stroke: ${ACCENT};
    stroke-width: 2;
    fill: none;
    filter: drop-shadow(0 0 3px rgba(37, 85, 230, 0.45));
    transition: opacity 180ms cubic-bezier(0.22, 1, 0.36, 1);
  }
  .poly.spot.is-strong {
    filter: drop-shadow(0 0 6px rgba(37, 85, 230, 0.65));
  }
  .veil { position: fixed; top: 0; left: 0; opacity: 0; pointer-events: none;
    -webkit-backdrop-filter: blur(3px) saturate(0.85); backdrop-filter: blur(3px) saturate(0.85);
    transition: opacity 220ms cubic-bezier(0.22, 1, 0.36, 1); }
  .veil.is-on { opacity: 1; }
  .chip, .hint { position: fixed; top: 0; left: 0; display: flex; align-items: center; gap: 6px;
    max-width: min(420px, calc(100vw - 16px)); padding: 5px 8px; border-radius: 6px; opacity: 0;
    font: 600 11px/1.2 "Instrument Sans", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
    letter-spacing: 0.01em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    box-shadow: 0 6px 16px -6px rgba(15, 22, 35, 0.4); transition: opacity 140ms cubic-bezier(0.22, 1, 0.36, 1); }
  .chip { background: ${ACCENT}; color: #fff; }
  .chip .meta { font-weight: 500; opacity: 0.8; }
  .hint { background: #0f1623; color: rgba(255, 255, 255, 0.86); font-weight: 500; }
  .hint .warn { color: #f8d691; }
  .chip.is-on, .hint.is-on { opacity: 1; }
  kbd { padding: 1px 5px; border-radius: 4px; background: rgba(255, 255, 255, 0.14); color: #fff;
    font: 600 10px/1.4 ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace; }
  .sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  @keyframes poly-flash {
    0% { stroke-opacity: 0; stroke-width: 5; }
    50% { stroke-opacity: 1; stroke-width: 3; }
    100% { stroke-opacity: 1; stroke-width: 2; }
  }
  @media (prefers-reduced-motion: reduce) {
    .poly, .spot-veil, .veil, .chip, .hint { transition: none; }
    .poly.select.is-flash { animation: none; }
  }`;

function makeSvg(tag, className, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  if (className) node.setAttribute('class', className);
  if (attrs) {
    for (const key of Object.keys(attrs)) node.setAttribute(key, attrs[key]);
  }
  return node;
}

function mountOverlay() {
  const host = document.createElement('waas-studio-overlay');
  const root = host.attachShadow({ mode: 'closed' });
  const style = document.createElement('style');
  style.textContent = OVERLAY_CSS;
  root.appendChild(style);
  const makeDiv = (className) => {
    const node = document.createElement('div');
    node.className = className;
    root.appendChild(node);
    return node;
  };

  ui.veils = [0, 1, 2, 3].map(() => makeDiv('veil'));

  // Toile vectorielle : contours polygonaux ajustés au texte et masque d'atténuation
  const svg = makeSvg('svg', 'overlay-canvas', { width: '100%', height: '100%' });
  const defs = makeSvg('defs');
  const mask = makeSvg('mask', null, { id: 'waas-spot-mask' });
  const maskBg = makeSvg('rect', null, { width: '100%', height: '100%', fill: 'white' });
  const spotCutout = makeSvg('path', 'spot-cutout', { fill: 'black', d: '' });
  mask.appendChild(maskBg);
  mask.appendChild(spotCutout);
  defs.appendChild(mask);
  svg.appendChild(defs);

  const spotVeil = makeSvg('rect', 'spot-veil', { width: '100%', height: '100%', mask: 'url(#waas-spot-mask)' });
  svg.appendChild(spotVeil);

  const hover = makeSvg('path', 'poly hover', { d: '' });
  const select = makeSvg('path', 'poly select', { d: '' });
  const spot = makeSvg('path', 'poly spot', { d: '' });
  svg.appendChild(hover);
  svg.appendChild(select);
  svg.appendChild(spot);

  root.appendChild(svg);

  ui.svg = svg;
  ui.spotCutout = spotCutout;
  ui.spotVeil = spotVeil;
  ui.hover = hover;
  ui.select = select;
  ui.spot = spot;

  ui.chip = makeDiv('chip');
  ui.hint = makeDiv('hint');
  ui.live = makeDiv('sr');
  ui.live.setAttribute('aria-live', 'polite');
  document.documentElement.appendChild(host);
  ui.host = host;
  ui.cursor = document.createElement('style');
  ui.cursor.setAttribute('data-waas-studio', '');
}

function place(node, rect) {
  if (!rect) {
    node.classList.remove('is-on');
    return;
  }
  const transform = `translate(${Math.round(rect.left)}px, ${Math.round(rect.top)}px)`;
  node.style.setProperty('--t', transform);
  node.style.transform = transform;
  node.style.width = `${Math.max(0, Math.round(rect.width))}px`;
  node.style.height = `${Math.max(0, Math.round(rect.height))}px`;
  node.classList.add('is-on');
}

function placePath(node, pathD) {
  if (!pathD) {
    node.setAttribute('d', '');
    node.classList.remove('is-on');
    return;
  }
  node.setAttribute('d', pathD);
  node.classList.add('is-on');
}

function placeSpotVeil(pathD) {
  if (!pathD) {
    ui.spotCutout.setAttribute('d', '');
    ui.spotVeil.classList.remove('is-on');
    return;
  }
  ui.spotCutout.setAttribute('d', pathD);
  ui.spotVeil.classList.toggle('is-strong', state.focusStyle === 'strong');
  ui.spotVeil.classList.add('is-on');
}

/* ----------------------------------------------------------------------
   Génération des contours polygonaux épousant fidèlement les lignes de texte
   ---------------------------------------------------------------------- */
function mergeRows(rects) {
  const valid = Array.from(rects || [])
    .filter((r) => r && (r.width > 0 || (r.right - r.left > 0)) && (r.height > 0 || (r.bottom - r.top > 0)))
    .map((r) => ({ left: r.left, top: r.top, right: r.right, bottom: r.bottom }));
  if (!valid.length) return [];
  valid.sort((a, b) => a.top - b.top || a.left - b.left);
  const rows = [];
  for (const r of valid) {
    const row = rows.find((existing) => {
      const overlap = Math.min(existing.bottom, r.bottom) - Math.max(existing.top, r.top);
      const minH = Math.min(existing.bottom - existing.top, r.bottom - r.top);
      return overlap >= minH * 0.45;
    });
    if (row) {
      row.left = Math.min(row.left, r.left);
      row.right = Math.max(row.right, r.right);
      row.top = Math.min(row.top, r.top);
      row.bottom = Math.max(row.bottom, r.bottom);
    } else {
      rows.push({ ...r });
    }
  }
  rows.sort((a, b) => a.top - b.top);
  for (let i = 0; i < rows.length - 1; i++) {
    if (rows[i + 1].top - rows[i].bottom < 12) {
      const seam = (rows[i].bottom + rows[i + 1].top) / 2;
      rows[i].bottom = seam;
      rows[i + 1].top = seam;
    }
  }
  return rows;
}

function buildPolygonForConnectedRows(rows, pad) {
  if (!rows.length) return '';
  if (rows.length === 1) {
    const r = rows[0];
    const left = Math.round(r.left - pad);
    const top = Math.round(r.top - pad);
    const right = Math.round(r.right + pad);
    const bottom = Math.round(r.bottom + pad);
    return `M ${left} ${top} H ${right} V ${bottom} H ${left} Z`;
  }
  const padded = rows.map((r, i) => ({
    left: Math.round(r.left - pad),
    right: Math.round(r.right + pad),
    top: Math.round(i === 0 ? r.top - pad : r.top),
    bottom: Math.round(i === rows.length - 1 ? r.bottom + pad : r.bottom)
  }));

  const points = [];
  points.push([padded[0].left, padded[0].top]);
  points.push([padded[0].right, padded[0].top]);

  for (let i = 0; i < padded.length; i++) {
    points.push([padded[i].right, padded[i].bottom]);
    if (i < padded.length - 1 && padded[i + 1].right !== padded[i].right) {
      points.push([padded[i + 1].right, padded[i].bottom]);
    }
  }

  points.push([padded[padded.length - 1].left, padded[padded.length - 1].bottom]);

  for (let i = padded.length - 1; i >= 0; i--) {
    points.push([padded[i].left, padded[i].top]);
    if (i > 0 && padded[i - 1].left !== padded[i].left) {
      points.push([padded[i - 1].left, padded[i].top]);
    }
  }

  let d = `M ${points[0][0]} ${points[0][1]}`;
  for (let i = 1; i < points.length; i++) {
    d += ` L ${points[i][0]} ${points[i][1]}`;
  }
  return d + ' Z';
}

function buildTextPolygon(rects, pad = 3) {
  const rows = mergeRows(rects);
  if (!rows.length) return '';
  const groups = [];
  let current = [rows[0]];
  for (let i = 1; i < rows.length; i++) {
    const prev = rows[i - 1];
    const curr = rows[i];
    const hasOverlap = Math.max(prev.left, curr.left) < Math.min(prev.right, curr.right);
    const closeY = curr.top - prev.bottom < 15;
    if (hasOverlap && closeY) {
      current.push(curr);
    } else {
      groups.push(current);
      current = [curr];
    }
  }
  groups.push(current);
  return groups.map((g) => buildPolygonForConnectedRows(g, pad)).join(' ');
}

function union(rects) {
  const list = Array.from(rects || []).filter((rect) => rect.width > 0 || rect.height > 0 || (rect.right - rect.left > 0));
  if (!list.length) return null;
  const left = Math.min(...list.map((r) => r.left));
  const top = Math.min(...list.map((r) => r.top));
  const right = Math.max(...list.map((r) => r.right));
  const bottom = Math.max(...list.map((r) => r.bottom));
  return { left, top, right, bottom, width: right - left, height: bottom - top };
}

function rectsOf(hit) {
  if (!hit) return [];
  if (!hit.el || !hit.el.isConnected) {
    hit.el = primary(hit.slot);
    if (!hit.el) return [];
  }
  const { slot, el } = hit;
  if (state.editing && state.editing.slot === slot) {
    const host = state.editing.host;
    if (host && host.isConnected) {
      try {
        const range = document.createRange();
        range.selectNodeContents(host);
        const rangeRects = Array.from(range.getClientRects()).filter((r) => r.width > 0 && r.height > 0);
        if (rangeRects.length) return rangeRects;
      } catch (error) {}
      const hostRects = Array.from(host.getClientRects()).filter((r) => r.width > 0 && r.height > 0);
      if (hostRects.length) return hostRects;
      return [host.getBoundingClientRect()];
    }
  }
  if (slot.kind === 'text') {
    let node = textNodeAt(el, slot.pos);
    if (!node && typeof isSplitHost === 'function' && isSplitHost(el)) {
      healSplittedElement(el);
      node = textNodeAt(el, slot.pos);
    }
    if (node) {
      const list = textRects(node);
      if (list.length) return list;
    }
  }
  return [el.getBoundingClientRect()];
}

function rectOf(hit) {
  return union(rectsOf(hit));
}

const inflate = (rect, by) => rect && {
  left: rect.left - by, top: rect.top - by, width: rect.width + by * 2, height: rect.height + by * 2,
  right: rect.right + by, bottom: rect.bottom + by
};

function placeVeils(hole) {
  if (!hole) {
    ui.veils.forEach((veil) => place(veil, null));
    return;
  }
  const width = window.innerWidth;
  const height = window.innerHeight;
  const top = Math.max(0, hole.top);
  const bottom = Math.min(height, hole.bottom);
  const middle = Math.max(0, bottom - top);
  const strips = [
    { left: 0, top: 0, width, height: top },
    { left: 0, top: bottom, width, height: Math.max(0, height - bottom) },
    { left: 0, top, width: Math.max(0, hole.left), height: middle },
    { left: hole.right, top, width: Math.max(0, width - hole.right), height: middle }
  ];
  ui.veils.forEach((veil, index) => place(veil, strips[index]));
}

function placeLabel(node, anchor, below) {
  if (!anchor) {
    node.classList.remove('is-on');
    return;
  }
  const height = 24;
  // Zone visible hors barres de défilement : une étiquette n'est jamais rognée.
  const viewWidth = document.documentElement.clientWidth || window.innerWidth;
  const viewHeight = document.documentElement.clientHeight || window.innerHeight;
  const top = below || anchor.top < height + 8 ? anchor.bottom + 8 : anchor.top - height - 6;
  const left = Math.max(8, Math.min(anchor.left, viewWidth - node.offsetWidth - 8));
  node.style.transform = `translate(${Math.round(left)}px, ${Math.round(Math.min(top, viewHeight - height - 4))}px)`;
  node.classList.add('is-on');
}

function setChip(slot, meta) {
  ui.chip.replaceChildren(document.createTextNode(slot.label), meta ? span('meta', meta) : '');
}

function span(className, text) {
  const node = document.createElement('span');
  node.className = className;
  node.textContent = text;
  return node;
}

function keyHint(...parts) {
  const nodes = [];
  parts.forEach(([key, text], index) => {
    if (index) nodes.push(document.createTextNode(' · '));
    const kbd = document.createElement('kbd');
    kbd.textContent = key;
    nodes.push(kbd, document.createTextNode(` ${text}`));
  });
  return nodes;
}

let frame = 0;
function schedule() {
  if (!frame) frame = window.requestAnimationFrame(render);
}

function render() {
  frame = 0;
  if (!ui.host) return;
  const editing = state.editing;
  const editMode = state.active && state.mode === 'edit';

  const hover = editMode && !editing && state.hover && !sameHit(state.hover, state.selected) ? state.hover : null;
  const hoverRects = hover ? rectsOf(hover) : [];
  placePath(ui.hover, hoverRects.length ? buildTextPolygon(hoverRects, 2.5) : null);

  const selected = editMode && !editing ? state.selected : null;
  const selectRects = selected ? rectsOf(selected) : [];
  const selectRect = selectRects.length ? union(selectRects) : null;
  placePath(ui.select, selectRects.length ? buildTextPolygon(selectRects, 2.5) : null);

  const spotRects = editMode && editing ? rectsOf(editing.hit) : [];
  const spotPath = spotRects.length ? buildTextPolygon(spotRects, SPOT_PADDING) : null;
  const spotRect = spotRects.length ? inflate(union(spotRects), SPOT_PADDING) : null;
  placePath(ui.spot, spotPath);
  placeSpotVeil(spotPath);
  placeVeils(spotRect && state.focusStyle === 'strong' ? spotRect : null);

  if (editing && spotRect) {
    setChip(editing.slot, 'Modification');
    placeLabel(ui.chip, spotRect, false);
    const length = editing.host.textContent.length;
    const parts = editing.slot.pre ? [['Ctrl ↵', 'valider'], ['Échap', 'annuler']]
      : [['↵', 'valider'], ['Échap', 'annuler'], ['Tab', 'suivant']];
    ui.hint.replaceChildren(...keyHint(...parts),
      length > MAX_VALUE * 0.8 ? span('warn', ` · ${length} / ${MAX_VALUE}`) : '');
    placeLabel(ui.hint, spotRect, true);
  } else if (selected && selectRect) {
    const inLink = selected.el && selected.el.closest && selected.el.closest('a[href]');
    let meta = selected.slot.kind === 'image' ? 'Double-clic pour remplacer' : 'Double-clic pour modifier';
    if (selected.slot.kind === 'attr' || (selected.slot.kind === 'text' && !selected.slot.ok)) meta = 'Modifiable depuis le panneau';
    if (inLink && selected.slot.kind === 'text') meta += ' · Ctrl+clic : suivre le lien';
    setChip(selected.slot, meta);
    placeLabel(ui.chip, selectRect, false);
    ui.hint.classList.remove('is-on');
  } else {
    ui.chip.classList.remove('is-on');
    ui.hint.classList.remove('is-on');
  }
  // Suivi continu pendant une sélection ou une édition (animations, carrousels du site).
  if (selected || editing) schedule();
}

function announce(message) {
  if (ui.live) ui.live.textContent = message;
}

function updateCursor() {
  if (!ui.cursor) return;
  const editMode = state.active && state.mode === 'edit';
  if (!editMode) {
    ui.cursor.remove();
    document.documentElement.removeAttribute('data-waas-studio');
    return;
  }
  document.documentElement.setAttribute('data-waas-studio', 'edit');
  let rules = 'html[data-waas-studio="edit"], html[data-waas-studio="edit"] * { cursor: default !important; }';
  rules += ' html[data-waas-studio="edit"] [data-split], html[data-waas-studio="edit"] .is-splitting { opacity: 1 !important; visibility: visible !important; }';
  rules += ' html[data-waas-studio="edit"] .split-w, html[data-waas-studio="edit"] .split-char, html[data-waas-studio="edit"] .split-line, html[data-waas-studio="edit"] .word, html[data-waas-studio="edit"] .char { opacity: 1 !important; visibility: visible !important; transform: none !important; animation: none !important; }';
  if (state.editing) {
    rules += ' waas-edit, [contenteditable] { cursor: text !important; }';
  } else if (state.hover) {
    const target = `[data-waas="${state.hover.slot.number}"]`;
    const cursor = state.hover.slot.kind === 'text' && state.hover.slot.ok ? 'text' : 'pointer';
    rules += ` ${target}, ${target} * { cursor: ${cursor} !important; }`;
  }
  ui.cursor.textContent = rules;
  if (!ui.cursor.isConnected) (document.head || document.documentElement).appendChild(ui.cursor);
}
