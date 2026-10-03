/*!
 * Studio — aperçu encadré et protocole du pont d'édition
 * v2 : poignée de main WAAS_BRIDGE_READY / WAAS_INIT, messages acceptés de la
 * seule iframe et de sa seule origine. v1 (vitrines non reconstruites) :
 * détecté par l'absence de poignée de main, messages historiques conservés.
 * Exposé sous window.StudioFrame.
 * 29th studio · © 2026
 */
(function (window, document) {
  'use strict';

  const LEGACY_AFTER_MS = 2500;
  const COMMIT_TIMEOUT_MS = 450;

  function createFrame(options) {
    const opts = options || {};
    const on = (name, ...args) => {
      const handler = opts.handlers && opts.handlers[name];
      return typeof handler === 'function' ? handler(...args) : undefined;
    };
    const frame = { iframe: null, origin: null, protocol: null, page: null, slots: [], editing: false, url: null };
    const commits = new Map();
    let legacyTimer = 0;
    let commitSeq = 0;

    frame.mount = function mount(url) {
      window.clearTimeout(legacyTimer);
      Object.assign(frame, { protocol: null, page: null, slots: [], editing: false, url, origin: new URL(url).origin });
      const iframe = document.createElement('iframe');
      iframe.id = 'studio-iframe';
      // Bac à sable sans « allow-top-navigation » : un script de la vitrine (gabarit,
      // service tiers) ne peut jamais rediriger l'onglet du Studio. Origine propre
      // conservée (cookies, stockage), formulaires et nouveaux onglets permis.
      iframe.setAttribute('sandbox',
        'allow-scripts allow-same-origin allow-forms allow-modals allow-popups allow-popups-to-escape-sandbox');
      iframe.src = url;
      iframe.title = opts.title || 'Aperçu éditable';
      iframe.setAttribute('allow', 'clipboard-write');
      iframe.addEventListener('load', onLoad);
      opts.wrapper.replaceChildren(iframe);
      frame.iframe = iframe;
      on('onLoading', url);
    };

    frame.unmount = function unmount() {
      window.clearTimeout(legacyTimer);
      frame.iframe = null;
      frame.protocol = null;
    };

    function onLoad() {
      on('onLoaded');
      if (frame.protocol != null) return;
      window.clearTimeout(legacyTimer);
      // Sans poignée de main : vitrine construite avant le moteur v2 (pont historique).
      legacyTimer = window.setTimeout(() => {
        if (frame.protocol == null && frame.iframe) {
          frame.protocol = 1;
          on('onLegacy');
        }
      }, LEGACY_AFTER_MS);
    }

    frame.send = function send(type, data) {
      const target = frame.iframe && frame.iframe.contentWindow;
      if (target && frame.origin) target.postMessage(Object.assign({ type }, data || {}), frame.origin);
    };

    frame.apply = function apply(changes) {
      if (!changes.length) return;
      if (frame.protocol === 2) frame.send('WAAS_APPLY', { changes });
      else if (frame.protocol === 1) changes.forEach((change) => frame.send('WAAS_UPDATE_PREVIEW', { xpath: change.key, newValue: change.value }));
    };

    frame.reveal = (key) => { if (frame.protocol === 2) frame.send('WAAS_REVEAL', { key }); };
    frame.setMode = (mode) => { if (frame.protocol === 2) frame.send('WAAS_SET_MODE', { mode }); };
    frame.setOptions = (values) => { if (frame.protocol === 2) frame.send('WAAS_SET_OPTIONS', values); };

    /** Valide l'édition en cours dans l'aperçu avant un enregistrement (réponse ou délai). */
    frame.commit = function commit() {
      if (frame.protocol !== 2) return Promise.resolve();
      commitSeq += 1;
      const requestId = commitSeq;
      return new Promise((resolve) => {
        const timer = window.setTimeout(() => {
          commits.delete(requestId);
          resolve();
        }, COMMIT_TIMEOUT_MS);
        commits.set(requestId, () => {
          window.clearTimeout(timer);
          resolve();
        });
        frame.send('WAAS_COMMIT', { requestId });
      });
    };

    const isText = (value) => typeof value === 'string';

    function onMessage(event) {
      if (!frame.iframe || event.source !== frame.iframe.contentWindow || event.origin !== frame.origin) return;
      const data = event.data;
      if (!data || typeof data !== 'object' || !isText(data.type)) return;
      switch (data.type) {
        case 'WAAS_BRIDGE_READY':
          window.clearTimeout(legacyTimer);
          Object.assign(frame, {
            protocol: 2, editing: false, page: isText(data.file) ? data.file : '',
            slots: Array.isArray(data.slots) ? data.slots.filter((slot) => slot && isText(slot.key)) : []
          });
          on('onReady', { file: frame.page, path: String(data.path || ''), title: String(data.title || ''),
            slots: frame.slots, scroll: data.scroll || null });
          break;
        case 'WAAS_SELECTED':
          on('onSelect', isText(data.key) ? data.key : null);
          break;
        case 'WAAS_EDIT_START':
        case 'WAAS_EDIT_END':
          frame.editing = data.type === 'WAAS_EDIT_START';
          on('onEditState', frame.editing, data.key);
          break;
        case 'WAAS_CHANGE':
          if (isText(data.key) && isText(data.value)) on('onChange', data.key, data.value);
          break;
        case 'WAAS_IMAGE_REQUEST':
          if (isText(data.key)) on('onImage', data);
          break;
        case 'WAAS_PANEL_REQUEST':
          if (isText(data.key)) on('onPanel', data.key);
          break;
        case 'WAAS_SHORTCUT':
          if (isText(data.action)) on('onShortcut', data.action);
          break;
        case 'WAAS_SCROLL':
          if (Number.isFinite(data.x) && Number.isFinite(data.y)) on('onScroll', { x: data.x, y: data.y });
          break;
        case 'WAAS_NAVIGATING':
          Object.assign(frame, { protocol: null, page: null, slots: [], editing: false });
          on('onNavigating', String(data.href || ''));
          break;
        case 'WAAS_COMMITTED': {
          const done = commits.get(data.requestId);
          commits.delete(data.requestId);
          if (done) done();
          break;
        }
        // Pont historique (v1)
        case 'WAAS_CONTENT_CHANGED':
          frame.protocol = 1;
          if (isText(data.xpath)) on('onLegacyChange', data.xpath, String(data.newValue == null ? '' : data.newValue));
          break;
        case 'WAAS_IMAGE_CLICKED':
          frame.protocol = 1;
          if (isText(data.xpath)) on('onLegacyImage', data.xpath, String(data.currentSrc || ''));
          break;
        default:
          break;
      }
    }

    window.addEventListener('message', onMessage);
    return frame;
  }

  window.StudioFrame = { createFrame };
})(window, document);
