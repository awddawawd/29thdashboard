/*!
 * Studio — contrôleur
 * Relie le modèle (StudioModel), l'aperçu encadré (StudioFrame) et le panneau
 * (StudioUI) : chargement, édition dans la page ou le panneau, annuler /
 * rétablir, publication (à chaud, sinon suivi du redémarrage), conflits,
 * préférences d'affichage. Page : Front/studio/index.html.
 * 29th studio · © 2026
 */
(function (window, document) {
  'use strict';

  const { h } = window.UI;
  const App = window.App;
  const SM = window.StudioModel;
  const SUI = window.StudioUI;
  const $ = (id) => document.getElementById(id);
  const siteId = App.param('site') || '';

  const PREF_FOCUS = 'waas.studio.focus';
  const PREF_PANEL = 'waas.studio.panel';
  const PREF_HINT = 'waas.studio.hint';
  const RESTART_TIMEOUT_MS = 120000;
  const RESTART_POLL_MS = 2000;
  const WIDE_SCREEN = 1280;

  const readPref = (key) => { try { return window.localStorage.getItem(key); } catch (error) { return null; } };
  const writePref = (key, value) => { try { window.localStorage.setItem(key, value); } catch (error) { /* navigation privée */ } };

  const model = SM.createModel();
  const state = {
    site: null, role: 'client', page: null, mode: 'edit',
    focusStyle: readPref(PREF_FOCUS) === 'strong' ? 'strong' : 'subtle',
    filter: 'page', query: '', limit: SUI.PAGE_SIZE, selectedKey: null,
    order: new Map(), onPage: new Map(), bridgeReady: false, legacy: false,
    scrolls: new Map(), saving: false, pendingReveal: null, forced: new Set(), refreshing: null,
    unpublished: false, panelStale: false
  };
  const ctx = { model, state, actions: {}, reviewRender: null };

  /** Aperçu d'une image du site : un chemin relatif se résout sur l'origine de la vitrine. */
  ctx.assetUrl = function assetUrl(value) {
    const raw = String(value || '');
    if (!raw || !SUI.safeImageUrl(raw)) return '';
    try {
      return new URL(raw, state.site && state.site.url ? state.site.url : window.location.href).href;
    } catch (error) {
      return '';
    }
  };

  /* ----------------------------------------------------------------------
     Aperçu
     ---------------------------------------------------------------------- */
  const frame = window.StudioFrame.createFrame({
    wrapper: $('studio-frame-wrapper'),
    title: 'Aperçu éditable de la vitrine',
    handlers: {
      onReady, onLegacy, onNavigating: () => { state.bridgeReady = false; renderPanel(); },
      onSelect: (xpath) => select(xpath ? SM.keyOf(state.page, xpath) : null, false),
      onChange: onCanvasChange,
      onImage: (request) => withItem(SM.keyOf(state.page, request.key), (item) => SUI.openImageDialog(ctx, item, request)),
      onPanel: (xpath) => withItem(SM.keyOf(state.page, xpath), (item) => revealInPanel(item.key)),
      onShortcut: (action) => {
        if (action === 'save') publish();
        else if (action === 'undo') undo();
        else if (action === 'redo') redo();
      },
      onScroll: (scroll) => state.scrolls.set(state.page, scroll),
      onLegacyChange, onLegacyImage: (xpath) => {
        const item = legacyItem(xpath);
        if (item) SUI.openImageDialog(ctx, item, null);
      }
    }
  });

  /** Adresse d'aperçu d'une page, toujours sur l'origine de la vitrine (sinon ``null``). */
  function pageUrl(file, edit) {
    const path = SM.pagePath(file);
    if (!path) return null;
    const site = new URL(state.site.url);
    const url = new URL(path, site);
    if (url.origin !== site.origin) return null;
    if (edit) url.searchParams.set('waas_edit', 'true');
    url.searchParams.set('waas_t', String(Date.now()));
    return url.toString();
  }

  function mountPage(file, options) {
    const opts = options || {};
    if (file && !SM.pagePath(file)) {
      window.Cockpit.toast({ title: 'Page non affichable', message: `Chemin de page refusé : ${file}`, tone: 'danger' });
      return;
    }
    state.page = file || state.page || 'index.html';
    state.bridgeReady = false;
    state.onPage = new Map();
    state.order = new Map();
    state.pendingReveal = opts.reveal || null;
    if (!opts.keepScroll) state.scrolls.delete(state.page);
    if (App.setParam) App.setParam('page', state.page);
    SUI.renderPageMenu(ctx);
    renderPanel();
    if (!state.site || !state.site.url) {
      renderUnavailable();
      return;
    }
    // Pont historique (v1) : le mode aperçu se fait sans paramètre d'édition.
    const url = pageUrl(state.page, !(state.legacy && state.mode === 'preview'));
    if (url) frame.mount(url);
    else renderUnavailable();
  }

  function renderUnavailable() {
    frame.unmount();
    window.UI.replace($('studio-frame-wrapper'), h('div', { class: 'ck-studio__empty' }, window.UI.empty({
      title: 'Aperçu indisponible',
      desc: state.site && state.site.running === false
        ? 'Le serveur de la vitrine est arrêté : l\'aperçu reviendra dès sa remise en ligne. Vous pouvez modifier le contenu depuis le panneau.'
        : 'La vitrine n\'a pas encore d\'adresse publiée. Modifiez le contenu depuis le panneau.',
      icon: 'monitor',
      actions: [window.UI.btn('Ouvrir le panneau', { size: 'sm', icon: 'list', onClick: () => togglePanel(true) })]
    })));
  }

  function onReady(info) {
    state.legacy = false;
    $('studio-banner').hidden = true;
    state.page = info.file || state.page;
    state.bridgeReady = true;
    state.onPage = new Map(info.slots.map((slot) => [slot.key, { ok: Boolean(slot.ok), kind: slot.kind }]));
    state.order = new Map(info.slots.map((slot, index) => [slot.key, index]));
    const values = [];
    model.pending.forEach((value, key) => {
      const item = model.item(key);
      if (item && item.file === state.page) values.push({ key: item.xpath, value });
    });
    const reveal = state.pendingReveal && model.item(state.pendingReveal);
    frame.send('WAAS_INIT', {
      mode: state.mode, focusStyle: state.focusStyle, values,
      scroll: state.scrolls.get(state.page) || null, reveal: reveal ? reveal.xpath : null
    });
    state.pendingReveal = null;
    if (App.setParam) App.setParam('page', state.page);
    SUI.renderPageMenu(ctx);
    renderPanel();
    // Éléments indexés depuis l'ouverture du Studio (vitrine redémarrée entre-temps).
    if (info.slots.some((slot) => !model.item(SM.keyOf(state.page, slot.key)))) refreshContent();
  }

  function onLegacy() {
    state.legacy = true;
    state.bridgeReady = false;
    const banner = $('studio-banner');
    const admin = state.role === 'admin';
    window.UI.replace(banner, window.UI.alert({
      tone: 'warn', bar: true, title: 'Éditeur de première génération',
      text: admin
        ? 'Cette vitrine a été construite avant le nouvel éditeur : seuls certains textes sont modifiables dans la page. Reconstruisez-la depuis sa fiche pour activer l\'édition complète et la publication sans coupure.'
        : 'Cette vitrine utilise une ancienne version de l\'éditeur : certains textes ne sont modifiables que depuis le panneau. Contactez l\'agence pour la mettre à jour.',
      actions: admin ? [window.UI.btn('Ouvrir la fiche', { size: 'xs', href: `/admin/sites/detail?id=${encodeURIComponent(siteId)}` })] : null
    }));
    banner.hidden = false;
    const values = [];
    model.pending.forEach((value, key) => {
      const item = model.item(key);
      if (item && item.file === state.page) values.push({ key: item.xpath, value });
    });
    frame.apply(values);
    renderPanel();
  }

  function legacyItem(xpath) {
    const matches = model.items.filter((item) => item.xpath === xpath && !item.detached);
    return matches.find((item) => item.file === state.page) || matches[0] || null;
  }

  function onLegacyChange(xpath, value) {
    const item = legacyItem(xpath);
    if (item) applyValue(item.key, value, { source: 'canvas' });
  }

  async function withItem(key, callback) {
    let item = model.item(key);
    if (!item) {
      await refreshContent();
      item = model.item(key);
    }
    if (item) callback(item);
    else window.Cockpit.toast({ title: 'Élément non indexé', message: 'Rechargez le Studio : la vitrine vient d\'être mise à jour.', tone: 'warn' });
  }

  function onCanvasChange(xpath, value) {
    withItem(SM.keyOf(state.page, xpath), (item) => applyValue(item.key, value, { source: 'canvas' }));
  }

  /* ----------------------------------------------------------------------
     Modifications
     ---------------------------------------------------------------------- */
  function applyValue(key, value, options) {
    const opts = options || {};
    const change = model.setValue(key, value, { coalesce: opts.coalesce, record: opts.record });
    if (!change) return;
    const item = model.item(key);
    if (opts.source !== 'canvas' && item.file === state.page) frame.apply([{ key: item.xpath, value }]);
    afterChange(key);
  }

  function afterChange(key) {
    const item = model.item(key);
    SUI.syncRow(ctx, key);
    if (item && item.seo) SUI.renderPageMenu(ctx);
    if (state.filter === 'dirty') {
      // Vue « Modifiés » : une ligne peut apparaître ou disparaître, mais jamais sous les
      // doigts de la personne qui tape — le rendu attend la sortie du champ.
      const items = $('studio-items');
      if (items.contains(document.activeElement)) state.panelStale = true;
      else renderPanel();
    }
    updateChrome();
    if (ctx.reviewRender) ctx.reviewRender();
  }

  function replay(step) {
    if (!step) return;
    const item = model.item(step.key);
    if (item && item.file === state.page) frame.apply([{ key: item.xpath, value: step.value }]);
    afterChange(step.key);
    select(step.key, true);
  }

  const undo = () => replay(model.undoStep());
  const redo = () => replay(model.redoStep());

  function select(key, reveal) {
    state.selectedKey = key;
    SUI.highlightRow(key, true);
    const item = key && model.item(key);
    if (reveal && item && item.file === state.page) frame.reveal(item.xpath);
  }

  function revealInPanel(key) {
    togglePanel(true);
    const item = model.item(key);
    if (item && item.file !== state.page && state.filter === 'page') setFilter('all');
    renderPanel();
    select(key, false);
    SUI.focusField(key);
  }

  /* ----------------------------------------------------------------------
     Publication
     ---------------------------------------------------------------------- */
  async function publish() {
    if (state.saving) return;
    // Verrou posé avant toute attente : un double déclenchement n'envoie jamais deux lots.
    state.saving = true;
    updateChrome();
    try {
      await frame.commit();                     // texte en cours de saisie dans l'aperçu
      if (!model.dirtyCount()) {
        if (state.unpublished) await republish();
        else if (!model.problems.size) window.Cockpit.toast({ title: 'Rien à publier', message: 'Toutes les modifications sont déjà en ligne.', tone: 'info', duration: 2600 });
        return;
      }
      const changes = model.changes(state.forced);
      const response = await App.api(`/api/v1/sites/${encodeURIComponent(siteId)}/content/batch`, {
        method: 'PUT', query: { auto_publish: 'true' }, body: { changes }
      });
      const results = Array.isArray(response.results) ? response.results : changes.map(() => ({ status: 'updated' }));
      const outcome = model.applyResults(results, changes);
      outcome.saved.forEach((key) => state.forced.delete(key));
      if (outcome.saved.length) state.unpublished = !response.published;
      report(outcome, response);
      renderPanel();
      SUI.renderPageMenu(ctx);
      if (response.published && response.publish_method === 'restart') await waitForRestart();
      else if (response.published && (outcome.conflicts.length || outcome.failed.length)) mountPage(state.page, { keepScroll: true });
    } catch (error) {
      App.toastError(error, 'Publication impossible');
    } finally {
      state.saving = false;
      updateChrome();
      if (ctx.reviewRender) ctx.reviewRender();
    }
  }

  /** Enregistré mais pas en ligne (publication en échec) : nouvelle tentative, sans rien renvoyer. */
  async function republish() {
    const response = await App.api(`/api/v1/sites/${encodeURIComponent(siteId)}/publish`, { method: 'POST' });
    state.unpublished = false;
    window.Cockpit.toast({
      title: 'Publié',
      message: response.method === 'restart' ? 'La vitrine redémarre avec le contenu enregistré.' : 'Le contenu enregistré est en ligne.',
      tone: 'good'
    });
    if (response.method === 'restart') await waitForRestart();
  }

  function report(outcome, response) {
    const saved = outcome.saved.length;
    if (saved) {
      const plural = saved > 1 ? 's' : '';
      let message = `${saved} modification${plural} en ligne.`;
      if (response.published && response.publish_method === 'restart') message = `${saved} modification${plural} enregistrée${plural} : la vitrine redémarre (quelques secondes).`;
      if (!response.published) message = `${saved} modification${plural} enregistrée${plural}. Mise en ligne différée${response.publish_detail ? ` : ${response.publish_detail}` : ''}.`;
      window.Cockpit.toast({ title: response.published ? 'Publié' : 'Enregistré', message, tone: response.published ? 'good' : 'warn' });
    }
    if (outcome.conflicts.length) {
      window.Cockpit.toast({
        title: `${outcome.conflicts.length} conflit${outcome.conflicts.length > 1 ? 's' : ''}`,
        message: 'Ces textes ont été modifiés entre-temps par quelqu\'un d\'autre : choisissez la version à garder.',
        tone: 'warn', duration: 8000
      });
      if (!ctx.reviewRender) SUI.openReviewDialog(ctx);
    }
    if (outcome.failed.length) {
      const problem = model.problem(outcome.failed[0]);
      window.Cockpit.toast({ title: 'Modification refusée', message: problem ? problem.detail : 'Valeur invalide.', tone: 'danger', duration: 7000 });
    }
  }

  /** Suivi du redémarrage (repli de la publication à chaud) : la vitrine répond-elle ? */
  async function waitForRestart() {
    const veil = $('studio-veil');
    veil.hidden = false;
    const started = Date.now();
    let online = false;
    await new Promise((resolve) => window.setTimeout(resolve, RESTART_POLL_MS));
    while (!online && Date.now() - started < RESTART_TIMEOUT_MS) {
      try {
        await fetch(new URL('/', state.site.url).toString(), { mode: 'no-cors', cache: 'no-store' });
        online = true;
      } catch (error) {
        await new Promise((resolve) => window.setTimeout(resolve, RESTART_POLL_MS));
      }
    }
    veil.hidden = true;
    if (!online) window.Cockpit.toast({ title: 'Vitrine lente à redémarrer', message: 'Rechargez l\'aperçu dans un instant.', tone: 'warn' });
    mountPage(state.page, { keepScroll: true });
  }

  /* ----------------------------------------------------------------------
     Actions du panneau et des dialogues
     ---------------------------------------------------------------------- */
  Object.assign(ctx.actions, {
    setValue: (key, value, options) => applyValue(key, value, options),
    focusItem: (key) => select(key, true),
    reveal: (key) => select(key, true),
    navigate(file, revealKey) {
      window.Cockpit.closeMenus();
      if (file === state.page && revealKey) select(revealKey, true);
      else mountPage(file, { reveal: revealKey });
    },
    discard(key) {
      const item = model.item(key);
      if (item) applyValue(key, item.value);
    },
    discardAll() {
      Array.from(model.pending.keys()).forEach((key) => ctx.actions.discard(key));
    },
    resetToSource(key) {
      const item = model.item(key);
      if (item && item.source != null) applyValue(key, item.source);
    },
    adoptRemote(key) {
      model.adoptRemote(key);
      const item = model.item(key);
      if (item && item.file === state.page) frame.apply([{ key: item.xpath, value: item.value }]);
      afterChange(key);
    },
    forceKeep(key) {
      state.forced.add(key);
      model.problems.delete(key);
      afterChange(key);
      publish();
    },
    async deleteDetached(key) {
      const item = model.item(key);
      if (!item) return;
      const ok = await window.UI.confirm({ title: 'Supprimer ce contenu ?', message: 'Ce texte ne correspond plus à aucun élément de la page. Il sera définitivement supprimé.', confirmLabel: 'Supprimer', tone: 'danger', icon: 'trash' });
      if (!ok) return;
      try {
        await App.api(`/api/v1/sites/${encodeURIComponent(siteId)}/content`, { method: 'DELETE', query: { file_path: item.file, xpath: item.xpath } });
        model.remove(key);
        renderPanel();
      } catch (error) {
        App.toastError(error, 'Suppression impossible');
      }
    },
    copy(value) {
      window.Cockpit.copyText(value).then(() => App.toastOk('Copié', 'Texte copié dans le presse-papier.'));
    },
    openImage: (key, request) => withItem(key, (item) => SUI.openImageDialog(ctx, item, request)),
    async upload(file) {
      const body = new FormData();
      body.append('file', file);
      const response = await App.api(`/api/v1/sites/${encodeURIComponent(siteId)}/upload`, { method: 'POST', body });
      return response.public_url;
    },
    publish
  });

  /* ----------------------------------------------------------------------
     Chrome : omnibar, panneau, préférences
     ---------------------------------------------------------------------- */
  function renderPanel() {
    SUI.renderPanel(ctx);
  }

  function updateChrome() {
    const count = model.dirtyCount();
    const problems = model.problems.size;
    const actionable = count || state.unpublished;
    $('btn-undo').disabled = !model.undo.length;
    $('btn-redo').disabled = !model.redo.length;
    const save = $('btn-save');
    save.disabled = !actionable || state.saving;
    save.classList.toggle('is-loading', state.saving);
    $('btn-save-count').textContent = count ? String(count) : '';
    $('btn-save-count').hidden = !count;
    const pill = $('studio-pending');
    pill.hidden = !count && !problems && !state.unpublished;
    let text = `${count} modification${count > 1 ? 's' : ''} non publiée${count > 1 ? 's' : ''}`;
    if (problems) text = `${problems} modification${problems > 1 ? 's' : ''} à vérifier`;
    else if (!count && state.unpublished) text = 'Enregistré, pas encore en ligne';
    $('studio-pending-text').textContent = text;
    pill.dataset.tone = problems || (!count && state.unpublished) ? 'warn' : 'accent';
    const pillButton = $('btn-save-pill');
    pillButton.disabled = !actionable || state.saving;
    pillButton.textContent = !count && state.unpublished ? 'Republier' : 'Publier';
    $('btn-review').hidden = !count && !problems;
  }

  function setFilter(value) {
    state.filter = value;
    state.limit = SUI.PAGE_SIZE;
    document.querySelectorAll('#studio-filter .ck-segmented__item').forEach((item) => {
      item.setAttribute('aria-pressed', String(item.dataset.value === value));
    });
  }

  function togglePanel(force) {
    const panel = $('studio-panel');
    const open = typeof force === 'boolean' ? force : panel.hidden;
    panel.hidden = !open;
    $('btn-panel').setAttribute('aria-pressed', String(open));
    writePref(PREF_PANEL, open ? '1' : '0');
  }

  function setMode(mode) {
    state.mode = mode === 'preview' ? 'preview' : 'edit';
    document.querySelectorAll('#studio-mode .ck-segmented__item').forEach((item) => {
      item.setAttribute('aria-pressed', String(item.dataset.value === state.mode));
    });
    if (frame.protocol === 2) frame.setMode(state.mode);
    else if (state.legacy && state.site && state.site.url) mountPage(state.page, { keepScroll: true });
  }

  function setFocusStyle(style) {
    state.focusStyle = style === 'strong' ? 'strong' : 'subtle';
    writePref(PREF_FOCUS, state.focusStyle);
    frame.setOptions({ focusStyle: state.focusStyle });
    document.querySelectorAll('[data-focus-style]').forEach((item) => {
      item.setAttribute('aria-checked', String(item.dataset.focusStyle === state.focusStyle));
    });
  }

  async function loadContent() {
    model.load(await App.api(`/api/v1/sites/${encodeURIComponent(siteId)}/content`));
  }

  function refreshContent() {
    if (!state.refreshing) {
      state.refreshing = loadContent()
        .then(() => { renderPanel(); SUI.renderPageMenu(ctx); })
        .catch(() => {})
        .finally(() => { state.refreshing = null; });
    }
    return state.refreshing;
  }

  async function loadLive() {
    const data = await App.api('/api/v1/portal/overview', { query: { site_id: siteId } });
    const live = data.site ? data.site.live_visitors : 0;
    $('studio-live').classList.toggle('is-idle', !live);
    $('studio-live-text').textContent = `${live} en direct`;
    return data;
  }

  function notFound(message) {
    window.UI.replace($('main'), h('main', { class: 'ck-auth' }, h('div', { class: 'ck-auth__card' }, window.UI.empty({
      title: 'Site introuvable', desc: message || 'Cette vitrine n\'existe pas ou ne vous est pas accessible.', icon: 'globe',
      actions: [window.UI.btn('Retour', { href: '/', variant: 'primary', size: 'sm' })]
    }))));
  }

  function showHint() {
    if (readPref(PREF_HINT)) return;
    const hint = $('studio-hint');
    hint.hidden = false;
    $('studio-hint-close').addEventListener('click', () => {
      hint.hidden = true;
      writePref(PREF_HINT, '1');
    });
  }

  function wire() {
    $('studio-devices').addEventListener('ck:change', (event) => { $('studio-frame-wrapper').dataset.device = event.detail.value; });
    $('studio-mode').addEventListener('ck:change', (event) => setMode(event.detail.value));
    $('studio-filter').addEventListener('ck:change', (event) => { setFilter(event.detail.value); renderPanel(); });
    $('btn-undo').addEventListener('click', undo);
    $('btn-redo').addEventListener('click', redo);
    $('btn-save').addEventListener('click', publish);
    $('btn-save-pill').addEventListener('click', publish);
    $('btn-review').addEventListener('click', () => SUI.openReviewDialog(ctx));
    $('btn-panel').addEventListener('click', () => togglePanel());
    $('btn-reload').addEventListener('click', () => mountPage(state.page, { keepScroll: true }));
    $('btn-shortcuts').addEventListener('click', SUI.openShortcutsDialog);
    document.querySelectorAll('[data-focus-style]').forEach((item) => {
      item.addEventListener('click', () => setFocusStyle(item.dataset.focusStyle));
    });
    $('studio-items').addEventListener('focusout', (event) => {
      if (state.panelStale && !$('studio-items').contains(event.relatedTarget)) {
        state.panelStale = false;
        window.setTimeout(renderPanel, 0);      // après le traitement du clic en cours
      }
    });
    let searchTimer = 0;
    $('studio-search').addEventListener('input', (event) => {
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(() => {
        state.query = event.target.value;
        state.limit = SUI.PAGE_SIZE;
        renderPanel();
      }, 140);
    });
    document.addEventListener('keydown', (event) => {
      if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
      const key = event.key.toLowerCase();
      const typing = event.target instanceof Element && event.target.closest('textarea, input');
      if (key === 's') {
        event.preventDefault();
        publish();
      } else if (!typing && key === 'z' && !event.shiftKey) {
        event.preventDefault();
        undo();
      } else if (!typing && (key === 'y' || (key === 'z' && event.shiftKey))) {
        event.preventDefault();
        redo();
      }
    });
    window.addEventListener('beforeunload', (event) => {
      if (model.dirtyCount()) {
        event.preventDefault();
        event.returnValue = '';
      }
    });
  }

  /* ----------------------------------------------------------------------
     Chargement
     ---------------------------------------------------------------------- */
  wire();
  setFocusStyle(state.focusStyle);
  const panelPref = readPref(PREF_PANEL);
  togglePanel(panelPref ? panelPref === '1' : window.innerWidth >= WIDE_SCREEN);

  App.boot(async ({ status }) => {
    if (!siteId) {
      notFound('Aucun site indiqué (paramètre « site » manquant).');
      return;
    }
    status('Ouverture de la vitrine…');
    const overview = await loadLive();
    if (!overview.site || overview.site.site_id !== siteId) {
      notFound();
      return;
    }
    state.site = overview.site;
    state.role = overview.user.role;
    document.title = `Studio · ${state.site.name}`;
    $('studio-name').textContent = state.site.name;
    const back = $('studio-back');
    back.href = state.role === 'admin' ? `/admin/sites/detail?id=${encodeURIComponent(siteId)}` : App.withSite('/portal', siteId);
    back.title = state.role === 'admin' ? 'Retour à la fiche du site' : 'Retour à votre espace client';
    const openLink = $('btn-open');
    openLink.href = window.Cockpit.safeHref(state.site.url || '#');
    openLink.toggleAttribute('aria-disabled', !state.site.url);

    status('Chargement du contenu éditable…');
    await loadContent();
    const pages = model.pages();
    const requested = App.param('page');
    updateChrome();
    if (!state.site.url) togglePanel(true);
    mountPage(pages.includes(requested) ? requested : (pages[0] || 'index.html'));
    showHint();
  }).then(() => App.poll(() => loadLive().catch(() => {}), 15000));
})(window, document);
