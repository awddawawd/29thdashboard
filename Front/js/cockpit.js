/*!
 * Cockpit DS — cockpit.js (noyau)
 * Icônes, formats, gabarits HTML auto-échappés, toasts, dialogues, menus,
 * onglets, segments, copie, horloge, uptime, zone de dépôt, tiroir mobile,
 * alignement sidebar/grille. Délégation d'événements : compatible htmx sans
 * ré-initialisation manuelle.
 * 29th studio · © 2026
 */
(function (window, document) {
  'use strict';

  const Cockpit = window.Cockpit || {};
  Cockpit.version = '1.1.0';

  /* ----------------------------------------------------------------------
     Utilitaires DOM
     ---------------------------------------------------------------------- */
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const SVG_NS = 'http://www.w3.org/2000/svg';

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }

  function reducedMotion() {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  function rafThrottle(fn) {
    let queued = false;
    return function throttled() {
      if (queued) return;
      queued = true;
      window.requestAnimationFrame(() => {
        queued = false;
        fn();
      });
    };
  }

  /* ----------------------------------------------------------------------
     Gabarits HTML sûrs : toute interpolation est échappée, sauf raw()
     ---------------------------------------------------------------------- */
  const RAW = Symbol('ck.raw');
  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

  function escapeHTML(value) {
    return String(value).replace(/[&<>"']/g, (ch) => ESC[ch]);
  }

  /* URL de lien sûre : http(s), mailto, tel, relatif ou ancre ; sinon « # » */
  function safeHref(value) {
    /* Comme l'analyseur d'URL : tabulations et retours à la ligne ignorés, contrôles C0 rognés */
    const url = String(value == null ? '' : value).replace(/[\t\n\r]/g, '').replace(/^[\u0000- ]+|[\u0000- ]+$/g, '');
    if (!url) return '#';
    const scheme = url.match(/^([a-z][a-z0-9+.-]*):/i);
    return !scheme || /^(https?|mailto|tel)$/i.test(scheme[1]) ? url : '#';
  }

  function raw(value) {
    return { [RAW]: true, value: String(value) };
  }

  function renderValue(value) {
    if (value == null || value === false) return '';
    if (Array.isArray(value)) return value.map(renderValue).join('');
    if (value[RAW]) return value.value;
    return escapeHTML(value);
  }

  function html(strings, ...values) {
    let out = strings[0];
    values.forEach((value, i) => {
      out += renderValue(value) + strings[i + 1];
    });
    return raw(out);
  }

  /* ----------------------------------------------------------------------
     Icônes (tracés Lucide, ISC) — trait 1.75, currentColor
     ---------------------------------------------------------------------- */
  const ICONS = {
    'layout-dashboard': '<rect width="7" height="9" x="3" y="3" rx="1"/><rect width="7" height="5" x="14" y="3" rx="1"/><rect width="7" height="9" x="14" y="12" rx="1"/><rect width="7" height="5" x="3" y="16" rx="1"/>',
    'layers': '<path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83z"/><path d="M2 12a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 12"/><path d="M2 17a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 17"/>',
    'globe': '<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>',
    'message': '<path d="M22 17a2 2 0 0 1-2 2H6.828a2 2 0 0 0-1.414.586l-2.202 2.202A.71.71 0 0 1 2 21.286V5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2z"/>',
    'credit-card': '<rect width="20" height="14" x="2" y="5" rx="2"/><line x1="2" x2="22" y1="10" y2="10"/>',
    'calendar-clock': '<path d="M16 14v2.2l1.6 1"/><path d="M16 2v3"/><path d="M21 7.338V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h2.338"/><path d="M3 9h5.859"/><path d="M8 2v3"/><circle cx="16" cy="16" r="6"/>',
    'calendar': '<path d="M8 2v4"/><path d="M16 2v4"/><rect width="18" height="18" x="3" y="4" rx="2"/><path d="M3 10h18"/>',
    'server': '<rect width="20" height="8" x="2" y="2" rx="2" ry="2"/><rect width="20" height="8" x="2" y="14" rx="2" ry="2"/><line x1="6" x2="6.01" y1="6" y2="6"/><line x1="6" x2="6.01" y1="18" y2="18"/>',
    'activity': '<path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"/>',
    'bell': '<path d="M10.268 21a2 2 0 0 0 3.464 0"/><path d="M3.262 15.326A1 1 0 0 0 4 17h16a1 1 0 0 0 .74-1.673C19.41 13.956 18 12.499 18 8A6 6 0 0 0 6 8c0 4.499-1.411 5.956-2.738 7.326"/>',
    'database': '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5V19A9 3 0 0 0 21 19V5"/><path d="M3 12A9 3 0 0 0 21 12"/>',
    'clock': '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    'history': '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>',
    'log-out': '<path d="m16 17 5-5-5-5"/><path d="M21 12H9"/><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>',
    'pencil': '<path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>',
    'external': '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    'arrow-up-right': '<path d="M7 7h10v10"/><path d="M7 17 17 7"/>',
    'arrow-down-right': '<path d="m7 7 10 10"/><path d="M17 7v10H7"/>',
    'arrow-right': '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
    'arrow-left': '<path d="m12 19-7-7 7-7"/><path d="M19 12H5"/>',
    'chevron-up': '<path d="m18 15-6-6-6 6"/>',
    'chevron-down': '<path d="m6 9 6 6 6-6"/>',
    'chevron-left': '<path d="m15 18-6-6 6-6"/>',
    'chevron-right': '<path d="m9 18 6-6-6-6"/>',
    'users': '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    'user': '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    'eye': '<path d="M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0"/><circle cx="12" cy="12" r="3"/>',
    'check': '<path d="M20 6 9 17l-5-5"/>',
    'check-circle': '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
    'x': '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    'x-circle': '<circle cx="12" cy="12" r="10"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/>',
    'alert-circle': '<circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/>',
    'alert-triangle': '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    'info': '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    'shield-check': '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>',
    'download': '<path d="M12 15V3"/><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/>',
    'upload': '<path d="M12 3v12"/><path d="m17 8-5-5-5 5"/><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>',
    'copy': '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
    'send': '<path d="M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z"/><path d="m21.854 2.147-10.94 10.939"/>',
    'save': '<path d="M15.2 3a2 2 0 0 1 1.4.6l3.8 3.8a2 2 0 0 1 .6 1.4V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M17 21v-7a1 1 0 0 0-1-1H8a1 1 0 0 0-1 1v7"/><path d="M7 3v4a1 1 0 0 0 1 1h7"/>',
    'refresh': '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
    'undo': '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 5.5 5.5a5.5 5.5 0 0 1-5.5 5.5H11"/>',
    'redo': '<path d="m15 14 5-5-5-5"/><path d="M20 9H9.5A5.5 5.5 0 0 0 4 14.5A5.5 5.5 0 0 0 9.5 20H13"/>',
    'monitor': '<rect width="20" height="14" x="2" y="3" rx="2"/><line x1="8" x2="16" y1="21" y2="21"/><line x1="12" x2="12" y1="17" y2="21"/>',
    'tablet': '<rect width="16" height="20" x="4" y="2" rx="2" ry="2"/><line x1="12" x2="12.01" y1="18" y2="18"/>',
    'smartphone': '<rect width="14" height="20" x="5" y="2" rx="2" ry="2"/><path d="M12 18h.01"/>',
    'git-branch': '<line x1="6" x2="6" y1="3" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>',
    'folder': '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
    'folder-up': '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/><path d="M12 10v6"/><path d="m9 13 3-3 3 3"/>',
    'archive': '<rect width="20" height="5" x="2" y="3" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/>',
    'plus': '<path d="M5 12h14"/><path d="M12 5v14"/>',
    'minus': '<path d="M5 12h14"/>',
    'settings': '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
    'sliders': '<line x1="21" x2="14" y1="4" y2="4"/><line x1="10" x2="3" y1="4" y2="4"/><line x1="21" x2="12" y1="12" y2="12"/><line x1="8" x2="3" y1="12" y2="12"/><line x1="21" x2="16" y1="20" y2="20"/><line x1="12" x2="3" y1="20" y2="20"/><line x1="14" x2="14" y1="2" y2="6"/><line x1="8" x2="8" y1="10" y2="14"/><line x1="16" x2="16" y1="18" y2="22"/>',
    'trash': '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/>',
    'lock': '<rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    'zap': '<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/>',
    'euro': '<path d="M4 10h12"/><path d="M4 14h9"/><path d="M19 6a7.7 7.7 0 0 0-5.2-2A7.9 7.9 0 0 0 6 12c0 4.4 3.5 8 7.8 8 2 0 3.8-.8 5.2-2"/>',
    'cpu': '<rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9" rx="1"/><path d="M15 2v2"/><path d="M15 20v2"/><path d="M2 15h2"/><path d="M2 9h2"/><path d="M20 15h2"/><path d="M20 9h2"/><path d="M9 2v2"/><path d="M9 20v2"/>',
    'memory': '<path d="M6 19v-3"/><path d="M10 19v-3"/><path d="M14 19v-3"/><path d="M18 19v-3"/><path d="M8 11V9"/><path d="M16 11V9"/><path d="M12 11V9"/><path d="M2 15h20"/><path d="M2 7a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v1.1a2 2 0 0 0 0 3.837V17a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-5.1a2 2 0 0 0 0-3.837Z"/>',
    'hard-drive': '<line x1="22" x2="2" y1="12" y2="12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/><line x1="6" x2="6.01" y1="16" y2="16"/><line x1="10" x2="10.01" y1="16" y2="16"/>',
    'terminal': '<path d="M12 19h8"/><path d="m4 17 6-6-6-6"/>',
    'search': '<path d="m21 21-4.34-4.34"/><circle cx="11" cy="11" r="8"/>',
    'grip': '<circle cx="9" cy="12" r="1"/><circle cx="9" cy="5" r="1"/><circle cx="9" cy="19" r="1"/><circle cx="15" cy="12" r="1"/><circle cx="15" cy="5" r="1"/><circle cx="15" cy="19" r="1"/>',
    'qr-code': '<rect width="5" height="5" x="3" y="3" rx="1"/><rect width="5" height="5" x="16" y="3" rx="1"/><rect width="5" height="5" x="3" y="16" rx="1"/><path d="M21 16h-3a2 2 0 0 0-2 2v3"/><path d="M21 21v.01"/><path d="M12 7v3a2 2 0 0 1-2 2H7"/><path d="M3 12h.01"/><path d="M12 3h.01"/><path d="M12 16v.01"/><path d="M16 12h1"/><path d="M21 12v.01"/><path d="M12 21v-1"/>',
    'mail': '<path d="m22 7-8.991 5.727a2 2 0 0 1-2.009 0L2 7"/><rect x="2" y="4" width="20" height="16" rx="2"/>',
    'file-text': '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>',
    'trending-up': '<path d="M16 7h6v6"/><path d="m22 7-8.5 8.5-5-5L2 17"/>',
    'trending-down': '<path d="M16 17h6v-6"/><path d="m22 17-8.5-8.5-5 5L2 7"/>',
    'bar-chart': '<path d="M3 3v16a2 2 0 0 0 2 2h16"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/>',
    'line-chart': '<path d="M3 3v16a2 2 0 0 0 2 2h16"/><path d="m19 9-5 5-4-4-3 3"/>',
    'pie-chart': '<path d="M21 12c.552 0 1.005-.449.95-.998a10 10 0 0 0-8.953-8.951c-.55-.055-.998.398-.998.95v8a1 1 0 0 0 1 1z"/><path d="M21.21 15.89A10 10 0 1 1 8 2.83"/>',
    'gauge': '<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
    'list': '<path d="M3 12h.01"/><path d="M3 18h.01"/><path d="M3 6h.01"/><path d="M8 12h13"/><path d="M8 18h13"/><path d="M8 6h13"/>',
    'table': '<path d="M12 3v18"/><rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18"/><path d="M3 15h18"/>',
    'grid': '<rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/><rect width="7" height="7" x="14" y="14" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/>',
    'megaphone': '<path d="m3 11 18-5v12L3 14v-3z"/><path d="M11.6 16.8a3 3 0 1 1-5.8-1.6"/>',
    'power': '<path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.77.04"/>',
    'play': '<polygon points="6 3 20 12 6 21 6 3"/>',
    'square': '<rect width="14" height="14" x="5" y="5" rx="2"/>',
    'hammer': '<path d="m15 12-8.373 8.373a1 1 0 1 1-3-3L12 9"/><path d="m18 15 4-4"/><path d="m21.5 11.5-1.914-1.914A2 2 0 0 1 19 8.172V7l-2.26-2.26a6 6 0 0 0-4.202-1.756L9 2.96l.92.82A6.18 6.18 0 0 1 12 8.4V10l2 2h1.172a2 2 0 0 1 1.414.586L18.5 14.5"/>',
    'printer': '<path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><path d="M6 9V3a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v6"/><rect x="6" y="14" width="12" height="8" rx="1"/>',
    'more': '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
    'link': '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
    'inbox': '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    'rocket': '<path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"/><path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/>',
    'menu': '<path d="M4 12h16"/><path d="M4 6h16"/><path d="M4 18h16"/>',
    'sparkle': '<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/>',
    'type': '<polyline points="4 7 4 4 20 4 20 7"/><line x1="9" x2="15" y1="20" y2="20"/><line x1="12" x2="12" y1="4" y2="20"/>',
    'palette': '<circle cx="13.5" cy="6.5" r=".5" fill="currentColor"/><circle cx="17.5" cy="10.5" r=".5" fill="currentColor"/><circle cx="8.5" cy="7.5" r=".5" fill="currentColor"/><circle cx="6.5" cy="12.5" r=".5" fill="currentColor"/><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.554C21.965 6.012 17.461 2 12 2z"/>',
    'box': '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>',
    'move': '<path d="M12 2v20"/><path d="m15 19-3 3-3-3"/><path d="m19 9 3 3-3 3"/><path d="M2 12h20"/><path d="m5 9-3 3 3 3"/><path d="m9 5 3-3 3 3"/>',
    'maximize': '<path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/>',
    'minimize': '<path d="M8 3v3a2 2 0 0 1-2 2H3"/><path d="M21 8h-3a2 2 0 0 1-2-2V3"/><path d="M3 16h3a2 2 0 0 1 2 2v3"/><path d="M16 21v-3a2 2 0 0 1 2-2h3"/>',
    'key': '<path d="m15.5 7.5 2.3 2.3a1 1 0 0 0 1.4 0l2.1-2.1a1 1 0 0 0 0-1.4L19 4"/><path d="m21 2-9.6 9.6"/><circle cx="7.5" cy="15.5" r="5.5"/>',
    'image': '<rect width="18" height="18" x="3" y="3" rx="2" ry="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/>',
    'wifi-off': '<path d="M12 20h.01"/><path d="M8.5 16.429a5 5 0 0 1 7 0"/><path d="M5 12.859a10 10 0 0 1 5.17-2.69"/><path d="M19 12.859a10 10 0 0 0-2.007-1.523"/><path d="M2 8.82a15 15 0 0 1 4.177-2.643"/><path d="M22 8.82a15 15 0 0 0-11.288-3.764"/><path d="m2 2 20 20"/>'
  };

  function iconMarkup(name, className) {
    const cls = 'ck-icon' + (className ? ' ' + className : '');
    return raw(`<svg class="${escapeHTML(cls)}" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${ICONS[name] || ''}</svg>`);
  }

  function icon(name, className) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', 'ck-icon' + (className ? ' ' + className : ''));
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    svg.innerHTML = ICONS[name] || '';
    return svg;
  }

  /** Injecte un sprite <symbol id="i-…"> : <svg class="ck-icon"><use href="#i-server"/></svg> */
  function injectSprite() {
    if (document.getElementById('ck-sprite')) return;
    const symbols = Object.keys(ICONS)
      .map((name) => `<symbol id="i-${name}" viewBox="0 0 24 24">${ICONS[name]}</symbol>`)
      .join('');
    const holder = document.createElement('div');
    holder.innerHTML = `<svg id="ck-sprite" xmlns="${SVG_NS}" aria-hidden="true" style="position:absolute;width:0;height:0;overflow:hidden">${symbols}</svg>`;
    document.body.insertBefore(holder.firstElementChild, document.body.firstChild);
  }

  /* ----------------------------------------------------------------------
     Formats (fr-FR)
     ---------------------------------------------------------------------- */
  const nfCache = new Map();
  function nf(options) {
    const key = JSON.stringify(options);
    if (!nfCache.has(key)) nfCache.set(key, new Intl.NumberFormat('fr-FR', options));
    return nfCache.get(key);
  }

  const fmt = {
    number: (n, digits = 0) => nf({ minimumFractionDigits: digits, maximumFractionDigits: digits }).format(n),
    compact: (n) => nf({ notation: 'compact', maximumFractionDigits: 1 }).format(n),
    currency: (n, digits = 0) => nf({ style: 'currency', currency: 'EUR', minimumFractionDigits: digits, maximumFractionDigits: digits }).format(n),
    percent: (n, digits = 0) => `${fmt.number(n, digits)} %`,
    date: (value, options) => new Intl.DateTimeFormat('fr-FR', options || { day: 'numeric', month: 'short' }).format(new Date(value)),
    time: (value) => new Intl.DateTimeFormat('fr-FR', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(value ? new Date(value) : new Date()),
    duration(seconds) {
      const s = Math.max(0, Math.floor(seconds));
      if (s < 60) return `${s}s`;
      const m = Math.floor(s / 60);
      if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`;
      const h = Math.floor(m / 60);
      if (h < 24) return `${h}h ${String(m % 60).padStart(2, '0')}m`;
      return `${Math.floor(h / 24)}j ${h % 24}h`;
    }
  };

  /* ----------------------------------------------------------------------
     Toasts — Cockpit.toast({ title, message, tone, duration })
     Compatible : window.showToast(message, type, title, duration)
     ---------------------------------------------------------------------- */
  const TONE_ALIAS = { success: 'good', good: 'good', error: 'danger', danger: 'danger', warning: 'warn', warn: 'warn', info: 'info', accent: 'info' };
  const TONE_ICON = { good: 'check', danger: 'alert-circle', warn: 'alert-triangle', info: 'info' };
  const EASE_IN_OUT = 'cubic-bezier(0.65, 0, 0.35, 1)';

  function toastRegion() {
    let region = $('.ck-toast-region');
    if (!region) {
      region = el('div', 'ck-toast-region');
      region.setAttribute('aria-live', 'polite');
      document.body.appendChild(region);
    }
    return region;
  }

  function armToastTimer(node, duration) {
    let remaining = duration;
    let startedAt = 0;
    let handle = 0;
    const hold = { hover: false, focus: false };
    const start = () => {
      if (hold.hover || hold.focus) return;
      startedAt = Date.now();
      handle = window.setTimeout(() => dismissToast(node), remaining);
      node.classList.remove('is-paused');
    };
    const pause = () => {
      window.clearTimeout(handle);
      remaining = Math.max(800, remaining - (Date.now() - startedAt));
      node.classList.add('is-paused');
    };
    const set = (key, on) => () => {
      const was = hold.hover || hold.focus;
      hold[key] = on;
      if (on && !was) pause();
      else if (!on) start();
    };
    node.addEventListener('mouseenter', set('hover', true));
    node.addEventListener('mouseleave', set('hover', false));
    node.addEventListener('focusin', set('focus', true));
    node.addEventListener('focusout', (e) => { if (!node.contains(e.relatedTarget)) set('focus', false)(); });
    start();
  }

  function toast(input, type, title, duration) {
    const opts = input && typeof input === 'object' ? input : { message: input, tone: type, title, duration };
    const message = opts.message == null ? '' : String(opts.message);
    if (!message && !opts.title) return null;
    const tone = TONE_ALIAS[opts.tone] || 'info';
    const ms = Number.isFinite(opts.duration) ? opts.duration : 4200;

    const node = el('div', 'ck-toast');
    node.dataset.tone = tone;
    node.setAttribute('role', tone === 'danger' ? 'alert' : 'status');
    node.style.setProperty('--t-duration', `${ms}ms`);

    const badge = el('span', 'ck-toast__icon');
    badge.appendChild(icon(TONE_ICON[tone]));
    const body = el('div', 'ck-toast__body');
    if (opts.title) body.appendChild(el('p', 'ck-toast__title', opts.title));
    if (message) body.appendChild(el('p', 'ck-toast__text', message));
    const close = el('button', 'ck-toast__close');
    close.type = 'button';
    close.setAttribute('aria-label', 'Fermer la notification');
    close.appendChild(icon('x'));
    close.addEventListener('click', () => dismissToast(node));
    node.append(badge, body, close);

    if (ms > 0) {
      node.appendChild(el('span', 'ck-toast__timer'));
      armToastTimer(node, ms);
    }
    toastRegion().appendChild(node);
    return node;
  }

  function dismissToast(target) {
    const node = typeof target === 'string' ? document.getElementById(target) : target;
    if (!node || node.classList.contains('is-leaving')) return;
    node.classList.add('is-leaving');
    const duration = reducedMotion() ? 1 : 200;
    const height = node.offsetHeight;
    const fade = node.animate(
      [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateX(16px)' }],
      { duration, easing: EASE_IN_OUT, fill: 'forwards' }
    );
    fade.finished
      .then(() => node.animate(
        [
          { height: `${height}px`, marginTop: '0px', paddingTop: '12px', paddingBottom: '12px', borderWidth: '1px' },
          { height: '0px', marginTop: '-8px', paddingTop: '0px', paddingBottom: '0px', borderWidth: '0px' }
        ],
        { duration, easing: EASE_IN_OUT, fill: 'forwards' }
      ).finished)
      .then(() => node.remove())
      .catch(() => node.remove());
  }

  /* ----------------------------------------------------------------------
     Dialogues (<dialog class="ck-dialog">)
     data-ck-open="id" · data-ck-close · data-ck-autoopen · data-ck-dispose
     ---------------------------------------------------------------------- */
  function resolve(target) {
    return typeof target === 'string' ? document.getElementById(target) : target;
  }

  function openDialog(target) {
    const dialog = resolve(target);
    if (!dialog || dialog.open) return;
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
  }

  function closeDialog(target, returnValue) {
    const dialog = resolve(target);
    if (!dialog) return;
    if (typeof dialog.close === 'function') dialog.close(returnValue || '');
    else dialog.removeAttribute('open');
  }

  function onDialogClosed(event) {
    const dialog = event.target;
    if (!(dialog instanceof HTMLElement) || !dialog.matches('dialog[data-ck-dispose]')) return;
    const container = dialog.parentElement;
    dialog.remove();
    if (container && container.id === 'modal-container') container.innerHTML = '';
  }

  function isBackdropClick(event, dialog) {
    const rect = dialog.getBoundingClientRect();
    return event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom;
  }

  /* ----------------------------------------------------------------------
     Menus déroulants
     Déclencheur : <button data-ck-menu="id" aria-haspopup="menu" aria-expanded="false">
     ---------------------------------------------------------------------- */
  function menuItems(menu) {
    return $$('.ck-menu__item:not([disabled])', menu);
  }

  function menuTrigger(menu) {
    return menu.id ? document.querySelector(`[data-ck-menu="${CSS.escape(menu.id)}"]`) : null;
  }

  function closeMenus(except) {
    $$('.ck-menu:not([hidden])').forEach((menu) => {
      if (menu === except) return;
      menu.hidden = true;
      const trigger = menuTrigger(menu);
      if (trigger) trigger.setAttribute('aria-expanded', 'false');
    });
  }

  function toggleMenu(trigger, force) {
    const menu = document.getElementById(trigger.getAttribute('data-ck-menu'));
    if (!menu) return;
    const open = typeof force === 'boolean' ? force : menu.hidden;
    closeMenus(menu);
    menu.hidden = !open;
    trigger.setAttribute('aria-expanded', String(open));
    if (open) {
      const first = menuItems(menu)[0];
      if (first) first.focus({ preventScroll: true });
    }
  }

  function moveFocus(items, current, key) {
    if (!items.length) return;
    const index = items.indexOf(current);
    let next = index;
    if (key === 'ArrowDown' || key === 'ArrowRight') next = (index + 1) % items.length;
    if (key === 'ArrowUp' || key === 'ArrowLeft') next = (index - 1 + items.length) % items.length;
    if (key === 'Home') next = 0;
    if (key === 'End') next = items.length - 1;
    items[next].focus();
    return items[next];
  }

  /* ----------------------------------------------------------------------
     Onglets & contrôles segmentés — émettent 'ck:change' { value }
     ---------------------------------------------------------------------- */
  function emitChange(source, value) {
    source.dispatchEvent(new CustomEvent('ck:change', { bubbles: true, detail: { value } }));
  }

  function selectTab(tab) {
    const list = tab.closest('[role="tablist"]');
    if (!list) return;
    $$('[role="tab"]', list).forEach((item) => {
      const selected = item === tab;
      item.setAttribute('aria-selected', String(selected));
      item.tabIndex = selected ? 0 : -1;
      const panel = document.getElementById(item.getAttribute('aria-controls'));
      if (panel) panel.hidden = !selected;
    });
    emitChange(list, tab.dataset.value || tab.id);
  }

  function selectSegment(item) {
    const group = item.closest('[data-ck-segmented]');
    if (!group) return;
    $$('.ck-segmented__item', group).forEach((seg) => seg.setAttribute('aria-pressed', String(seg === item)));
    emitChange(group, item.dataset.value || item.textContent.trim());
  }

  /* ----------------------------------------------------------------------
     Copie dans le presse-papier — data-ck-copy="texte" | data-ck-copy-target="#sel"
     ---------------------------------------------------------------------- */
  function fallbackCopy(text) {
    const area = el('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.cssText = 'position:fixed;top:-100px;opacity:0';
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    return ok ? Promise.resolve() : Promise.reject(new Error('copy failed'));
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).catch(() => fallbackCopy(text));
    }
    return fallbackCopy(text);
  }

  function handleCopy(trigger) {
    const selector = trigger.getAttribute('data-ck-copy-target');
    const source = selector ? document.querySelector(selector) : null;
    const text = source ? (source.value != null ? source.value : source.textContent) : trigger.getAttribute('data-ck-copy');
    if (!text) return;
    copyText(text)
      .then(() => toast({ title: 'Copié', message: trigger.getAttribute('data-ck-copy-label') || 'Contenu copié dans le presse-papier.', tone: 'good', duration: 2400 }))
      .catch(() => toast({ title: 'Copie impossible', message: 'Votre navigateur a bloqué l’accès au presse-papier.', tone: 'danger' }));
  }

  /* ----------------------------------------------------------------------
     Horloge & uptime en direct
     ---------------------------------------------------------------------- */
  function tickClocks() {
    const now = fmt.time();
    $$('[data-ck-clock], #sidebar-live-clock').forEach((node) => {
      if (node.textContent !== now) node.textContent = now;
    });
    const epoch = Math.floor(Date.now() / 1000);
    $$('[data-started-at]').forEach((node) => {
      const started = parseInt(node.getAttribute('data-started-at'), 10);
      if (started > 0) node.textContent = fmt.duration(epoch - started);
    });
  }

  /* ----------------------------------------------------------------------
     Zone de dépôt — [data-ck-dropzone] contenant un input[type=file]
     ---------------------------------------------------------------------- */
  function describeFiles(files, isFolder) {
    if (!files || !files.length) return '';
    if (files.length === 1 && !isFolder) return `${files[0].name} (${fmt.number(files[0].size / 1024)} Ko)`;
    return `Dossier (${fmt.number(files.length)} fichiers)`;
  }

  function onFilesChosen(zone, files, isFolder) {
    const label = describeFiles(files, isFolder);
    const chip = zone.parentElement && zone.parentElement.querySelector('[data-ck-file-name]');
    if (chip && label) {
      chip.textContent = label;
      const holder = chip.closest('.ck-file-chip');
      if (holder) holder.hidden = false;
    }
    zone.dispatchEvent(new CustomEvent('ck:files', { bubbles: true, detail: { files, isFolder } }));
  }

  function onDrag(event) {
    const zone = event.target.closest && event.target.closest('[data-ck-dropzone]');
    if (!zone) return;
    event.preventDefault();
    const entering = event.type === 'dragenter' || event.type === 'dragover';
    zone.classList.toggle('is-dragover', entering);
    if (event.type === 'drop' && event.dataTransfer && event.dataTransfer.files.length) {
      const input = zone.querySelector('input[type="file"]:not([webkitdirectory])');
      if (input) input.files = event.dataTransfer.files;
      onFilesChosen(zone, event.dataTransfer.files, false);
    }
  }

  /* ----------------------------------------------------------------------
     Alignement au pixel : sidebar = 1 colonne de la grille de widgets
     ---------------------------------------------------------------------- */
  function syncScrollbar() {
    $$('.ck-app').forEach((app) => {
      const content = $('.ck-content', app);
      if (!content) return;
      const width = `${Math.max(0, content.offsetWidth - content.clientWidth)}px`;
      if (app.style.getPropertyValue('--ck-scrollbar-w') !== width) {
        app.style.setProperty('--ck-scrollbar-w', width);
      }
    });
  }

  /* ----------------------------------------------------------------------
     Délégation globale
     ---------------------------------------------------------------------- */
  function onClick(event) {
    const target = event.target;
    if (!(target instanceof Element)) return;

    const opener = target.closest('[data-ck-open]');
    if (opener) {
      event.preventDefault();
      openDialog(opener.getAttribute('data-ck-open'));
      return;
    }
    const closer = target.closest('[data-ck-close]');
    if (closer && closer.closest('dialog')) {
      event.preventDefault();
      closeDialog(closer.closest('dialog'), closer.getAttribute('data-ck-close'));
      return;
    }
    if (target.matches('dialog.ck-dialog[open]:not([data-ck-static])') && isBackdropClick(event, target)) {
      closeDialog(target);
      return;
    }

    const menuButton = target.closest('[data-ck-menu]');
    if (menuButton) {
      event.preventDefault();
      toggleMenu(menuButton);
      return;
    }
    if (!target.closest('.ck-menu')) closeMenus();
    if (target.closest('.ck-menu__item')) closeMenus();

    const tab = target.closest('[data-ck-tabs] [role="tab"]');
    if (tab) selectTab(tab);

    const segment = target.closest('[data-ck-segmented] .ck-segmented__item');
    if (segment) selectSegment(segment);

    const copier = target.closest('[data-ck-copy], [data-ck-copy-target]');
    if (copier) {
      event.preventDefault();
      handleCopy(copier);
    }

    const picker = target.closest('[data-ck-pick]');
    if (picker) {
      const input = document.querySelector(picker.getAttribute('data-ck-pick'));
      if (input) input.click();
    }

    if (target.closest('[data-ck-print]')) window.print();

    const navToggle = target.closest('[data-ck-nav-toggle], .ck-scrim');
    if (navToggle) {
      const app = navToggle.closest('.ck-app') || $('.ck-app');
      if (app) app.classList.toggle('is-nav-open', navToggle.matches('[data-ck-nav-toggle]') ? undefined : false);
    }
  }

  function onKeydown(event) {
    const target = event.target;
    if (event.key === 'Escape') {
      const openMenu = $('.ck-menu:not([hidden])');
      if (openMenu) {
        const trigger = menuTrigger(openMenu);
        closeMenus();
        if (trigger) trigger.focus();
      }
      $$('.ck-app.is-nav-open').forEach((app) => app.classList.remove('is-nav-open'));
      return;
    }
    if (!(target instanceof Element)) return;
    const arrows = ['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Home', 'End'];
    const menu = target.closest('.ck-menu');
    if (menu && arrows.includes(event.key)) {
      event.preventDefault();
      moveFocus(menuItems(menu), target.closest('.ck-menu__item'), event.key);
      return;
    }
    const tablist = target.closest('[data-ck-tabs][role="tablist"]');
    if (tablist && arrows.includes(event.key)) {
      event.preventDefault();
      const next = moveFocus($$('[role="tab"]', tablist), target.closest('[role="tab"]'), event.key);
      if (next) selectTab(next);
      return;
    }
    if ((event.key === 'Enter' || event.key === ' ') && target.matches('[data-ck-pick][role="button"]')) {
      event.preventDefault();
      target.click();
    }
  }

  function onChange(event) {
    const input = event.target;
    if (!(input instanceof HTMLInputElement) || input.type !== 'file') return;
    const zone = input.closest('[data-ck-dropzone]') || document.querySelector(`[data-ck-dropzone="${CSS.escape(input.id)}"]`);
    if (zone) onFilesChosen(zone, input.files, input.hasAttribute('webkitdirectory'));
  }

  /* ----------------------------------------------------------------------
     Initialisation (idempotente) — appelée au chargement et après htmx
     ---------------------------------------------------------------------- */
  function init(root) {
    const scope = root && root.querySelectorAll ? root : document;
    $$('[data-ck-tabs][role="tablist"]', scope).forEach((list) => {
      const tabs = $$('[role="tab"]', list);
      const current = tabs.find((t) => t.getAttribute('aria-selected') === 'true') || tabs[0];
      if (current && !list.dataset.ckReady) {
        list.dataset.ckReady = '1';
        selectTab(current);
      }
    });
    const dialogs = $$('dialog.ck-dialog[data-ck-autoopen]:not([open])', scope);
    if (scope.matches && scope.matches('dialog.ck-dialog[data-ck-autoopen]:not([open])')) dialogs.push(scope);
    dialogs.forEach(openDialog);
    tickClocks();
    syncScrollbar();
    if (Cockpit.charts && typeof Cockpit.charts.mount === 'function') Cockpit.charts.mount(scope);
  }

  const onResize = rafThrottle(syncScrollbar);

  document.addEventListener('click', onClick);
  document.addEventListener('keydown', onKeydown);
  document.addEventListener('change', onChange);
  ['dragenter', 'dragover', 'dragleave', 'drop'].forEach((type) => document.addEventListener(type, onDrag));
  document.addEventListener('close', onDialogClosed, true);
  document.addEventListener('htmx:afterSettle', (event) => init(event.detail && event.detail.elt));
  document.addEventListener('showToast', (event) => {
    const detail = event.detail || {};
    if (typeof detail === 'string') toast(detail);
    else toast({ message: detail.message, tone: detail.type || detail.tone, title: detail.title, duration: detail.duration });
  });
  window.addEventListener('resize', onResize);
  window.setInterval(tickClocks, 1000);

  /* Sprite d'icônes : une seule source (ICONS), injecté dès que <body> existe */
  if (document.body) injectSprite();
  else document.addEventListener('DOMContentLoaded', injectSprite);

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => init(document));
  else init(document);

  /* ----------------------------------------------------------------------
     API publique
     ---------------------------------------------------------------------- */
  Object.assign(Cockpit, {
    $, $$, el, html, raw, escapeHTML, safeHref, icon, iconMarkup, injectSprite, icons: ICONS,
    fmt, toast, dismissToast, openDialog, closeDialog, toggleMenu, closeMenus,
    selectTab, copyText, syncScrollbar, reducedMotion, rafThrottle, init
  });
  window.Cockpit = Cockpit;

  // Compatibilité avec le code existant du dashboard
  window.showToast = (message, type, title, duration) => toast(message, type, title, duration);
  window.dismissToast = dismissToast;
})(window, document);
