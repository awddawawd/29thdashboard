/*!
 * Studio — panneau de contenu et dialogues
 * Rendu DOM sans innerHTML (UI.h). Les actions remontent au contrôleur via
 * ctx.actions ; l'état vient de ctx.model (StudioModel) et ctx.state.
 * Exposé sous window.StudioUI.
 * 29th studio · © 2026
 */
(function (window, document) {
  'use strict';

  const { h } = window.UI;
  const SM = window.StudioModel;
  const PAGE_SIZE = 150;
  const MAX_FILE_BYTES = 5 * 1024 * 1024;
  const IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/webp', 'image/svg+xml', 'image/gif'];
  const $ = (id) => document.getElementById(id);

  const iconBtn = (label, icon, onClick, extra) => window.UI.btn(label, Object.assign({
    variant: 'ghost', size: 'xs', icon, iconOnly: true, onClick
  }, extra || {}));

  function safeImageUrl(value) {
    const url = String(value || '').trim();
    if (!url || url.length > 2048 || /[\s"<>`\\\u0000-\u001f\u007f]/.test(url)) return null;
    const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(url);
    return !scheme || /^https?$/i.test(scheme[1]) ? url : null;
  }

  function autosize(field) {
    field.style.height = 'auto';
    field.style.height = `${Math.min(field.scrollHeight + 2, 320)}px`;
  }

  /* ----------------------------------------------------------------------
     Panneau : sélection, ordre, regroupement
     ---------------------------------------------------------------------- */
  function visibleItems(ctx) {
    const { model, state } = ctx;
    const query = state.query.trim().toLowerCase();
    return model.items.filter((item) => {
      if (state.filter === 'page' && item.file !== state.page) return false;
      if (state.filter === 'dirty' && !model.isDirty(item.key) && !model.problem(item.key)) return false;
      if (!query) return true;
      return item.label.toLowerCase().includes(query) || model.current(item.key).toLowerCase().includes(query)
        || item.file.toLowerCase().includes(query);
    });
  }

  function orderOf(ctx, item) {
    const index = item.file === ctx.state.page ? ctx.state.order.get(item.xpath) : undefined;
    return index === undefined ? Number.MAX_SAFE_INTEGER : index;
  }

  function sortItems(ctx, items) {
    return items.slice().sort((a, b) => SM.comparePages(a.file, b.file) || orderOf(ctx, a) - orderOf(ctx, b)
      || a.xpath.localeCompare(b.xpath, 'fr', { numeric: true }));
  }

  function groupsOf(ctx, items) {
    const active = items.filter((item) => !item.detached);
    const detached = items.filter((item) => item.detached);
    const groups = [];
    if (ctx.state.filter === 'page') {
      const seo = active.filter((item) => item.seo);
      if (seo.length) groups.push({ title: 'Référencement', hint: 'Onglet du navigateur et résultats Google', items: seo });
      groups.push({ title: 'Contenu de la page', items: active.filter((item) => !item.seo) });
    } else {
      ctx.model.pages().forEach((file) => {
        const pageItems = active.filter((item) => item.file === file);
        if (pageItems.length) groups.push({ title: ctx.model.pageTitle(file), hint: SM.displayPath(file), items: pageItems, file });
      });
    }
    if (detached.length) groups.push({ title: 'Contenus détachés', detached: true, items: detached,
      hint: 'Textes personnalisés dont l\'élément a disparu du gabarit : copiez-les où il faut, puis supprimez-les.' });
    return groups;
  }

  function renderPanel(ctx) {
    const items = sortItems(ctx, visibleItems(ctx));
    $('studio-count').textContent = String(items.filter((item) => !item.detached).length);
    const container = $('studio-items');
    if (!items.length) {
      const empty = ctx.state.query ? 'Aucun élément ne correspond à la recherche.'
        : ctx.state.filter === 'dirty' ? 'Aucune modification en attente : tout est publié.'
          : 'Le contenu éditable apparaît après le premier démarrage de la vitrine.';
      window.UI.replace(container, h('div', { class: 'ck-studio__panel-empty' },
        window.UI.empty({ title: 'Aucun élément', desc: empty, icon: 'type', small: true })));
      return;
    }
    let budget = ctx.state.limit;
    const nodes = groupsOf(ctx, items).map((group) => {
      const rows = group.items.slice(0, Math.max(0, budget)).map((item) => renderRow(ctx, item));
      budget -= rows.length;
      if (!rows.length) return null;
      const head = h('header', { class: 'ck-studio__group-head' },
        h('span', { class: 'ck-studio__group-title' }, group.title),
        group.hint ? h('span', { class: 'ck-studio__group-hint' }, group.hint) : null,
        group.file && group.file !== ctx.state.page
          ? window.UI.btn('Ouvrir', { variant: 'ghost', size: 'xs', icon: 'arrow-right', onClick: () => ctx.actions.navigate(group.file) })
          : null);
      if (group.detached) {
        return h('details', { class: 'ck-studio__group ck-studio__group--detached' },
          h('summary', null, head), rows);
      }
      return h('section', { class: 'ck-studio__group', 'aria-label': group.title }, head, rows);
    });
    const hidden = items.length - ctx.state.limit;
    if (hidden > 0) {
      nodes.push(h('div', { class: 'ck-studio__more' }, window.UI.btn(`Afficher ${Math.min(hidden, PAGE_SIZE)} de plus`, {
        size: 'sm', variant: 'secondary', onClick: () => { ctx.state.limit += PAGE_SIZE; renderPanel(ctx); }
      })));
    }
    window.UI.replace(container, nodes);
    if (ctx.state.selectedKey) highlightRow(ctx.state.selectedKey, false);
  }

  /* ----------------------------------------------------------------------
     Ligne d'élément
     ---------------------------------------------------------------------- */
  function statusBadges(ctx, item) {
    const { model, state } = ctx;
    const problem = model.problem(item.key);
    const onPage = item.file === state.page && state.bridgeReady ? state.onPage.get(item.xpath) : undefined;
    const badges = [];
    if (problem && problem.status === 'conflict') badges.push(window.UI.badge('Conflit', 'warn', { size: 'sm' }));
    else if (problem) badges.push(window.UI.badge('Refusé', 'danger', { size: 'sm' }));
    else if (model.isDirty(item.key)) badges.push(window.UI.badge('Modifié', 'accent', { size: 'sm' }));
    if (!item.detached && item.file === state.page && state.bridgeReady) {
      if (onPage === undefined) badges.push(window.UI.badge('Absent de la page', 'neutral', { size: 'sm', title: 'Cet élément n\'apparaît pas dans la page affichée (gabarit modifié ou contenu masqué).' }));
      else if (!onPage.ok && item.kind === 'text') badges.push(window.UI.badge('Panneau seul', 'neutral', { size: 'sm', title: 'Texte réécrit par un script de la page : modifiable ici uniquement.' }));
    }
    return badges;
  }

  function rowActions(ctx, item) {
    const { model, state } = ctx;
    const value = model.current(item.key);
    const actions = [];
    const located = item.file === state.page && state.bridgeReady && state.onPage.has(item.xpath);
    if (located) actions.push(iconBtn('Voir dans la page', 'eye', () => ctx.actions.reveal(item.key, true)));
    else if (!item.detached && item.file !== state.page) {
      actions.push(iconBtn('Ouvrir la page', 'arrow-right', () => ctx.actions.navigate(item.file, item.key)));
    }
    if (model.isDirty(item.key)) actions.push(iconBtn('Annuler cette modification', 'undo', () => ctx.actions.discard(item.key)));
    else if (item.source != null && !item.detached && value !== item.source) {
      actions.push(iconBtn('Revenir au texte d\'origine du gabarit', 'history', () => ctx.actions.resetToSource(item.key)));
    }
    if (item.detached) {
      actions.push(iconBtn('Copier le texte', 'copy', () => ctx.actions.copy(value)));
      actions.push(iconBtn('Supprimer ce contenu détaché', 'trash', () => ctx.actions.deleteDetached(item.key)));
    }
    return actions;
  }

  function metaLine(ctx, item) {
    const parts = [];
    if (ctx.state.filter !== 'page') parts.push(SM.displayPath(item.file));
    if (item.updatedBy || item.updatedAt) {
      parts.push(`Modifié${item.updatedBy ? ` par ${item.updatedBy}` : ''}${item.updatedAt ? ` · ${window.UI.fmt.relative(item.updatedAt)}` : ''}`);
    }
    const limit = SM.seoLimit(item);
    const length = ctx.model.current(item.key).length;
    const counter = limit ? h('span', { class: 'ck-studio__counter', 'data-over': length > limit ? 'true' : null }, `${length} / ${limit}`) : null;
    if (!parts.length && !counter) return null;
    return h('div', { class: 'ck-studio__meta', 'data-part': 'meta' }, h('span', null, parts.join(' · ')), counter);
  }

  function problemBox(ctx, item) {
    const problem = ctx.model.problem(item.key);
    if (!problem) return null;
    if (problem.status !== 'conflict') {
      return window.UI.alert({ tone: 'danger', inline: true, text: problem.detail || 'Modification refusée.' });
    }
    return window.UI.alert({
      tone: 'warn', inline: true, title: 'Modifié entre-temps par quelqu\'un d\'autre',
      text: `En ligne : « ${problem.current || '(vide)'} »`,
      actions: [
        window.UI.btn('Garder la mienne', { size: 'xs', variant: 'primary', onClick: () => ctx.actions.forceKeep(item.key) }),
        window.UI.btn('Prendre la version en ligne', { size: 'xs', variant: 'ghost', onClick: () => ctx.actions.adoptRemote(item.key) })
      ]
    });
  }

  function fieldFor(ctx, item) {
    const value = ctx.model.current(item.key);
    if (item.kind === 'image') {
      return h('div', { class: 'ck-studio__image', 'data-part': 'field' },
        h('img', { class: 'ck-studio__thumb', alt: '', loading: 'lazy', src: ctx.assetUrl(value) }),
        window.UI.btn('Remplacer', { size: 'xs', icon: 'upload', onClick: () => ctx.actions.openImage(item.key, null) }));
    }
    if (item.detached) {
      return h('blockquote', { class: 'ck-studio__detached', 'data-part': 'field' }, value || '(vide)');
    }
    const field = h('textarea', {
      class: 'ck-textarea ck-studio__field', rows: 1, maxlength: 5000, spellcheck: 'true',
      'aria-label': `${item.label}${item.file !== ctx.state.page ? ` (${SM.displayPath(item.file)})` : ''}`,
      'data-part': 'field'
    });
    field.value = value;
    let timer = 0;
    field.addEventListener('input', () => {
      autosize(field);
      window.clearTimeout(timer);
      timer = window.setTimeout(() => ctx.actions.setValue(item.key, field.value, { coalesce: true, source: 'panel' }), 220);
    });
    field.addEventListener('focus', () => ctx.actions.focusItem(item.key));
    field.addEventListener('blur', () => {
      window.clearTimeout(timer);
      if (field.value !== ctx.model.current(item.key)) ctx.actions.setValue(item.key, field.value, { coalesce: true, source: 'panel' });
    });
    window.requestAnimationFrame(() => autosize(field));
    return field;
  }

  function renderRow(ctx, item) {
    const { model, state } = ctx;
    const classes = ['ck-studio__item'];
    if (model.isDirty(item.key)) classes.push('is-dirty');
    if (model.problem(item.key)) classes.push('is-problem');
    if (state.selectedKey === item.key) classes.push('is-selected');
    if (item.detached) classes.push('is-detached');
    const typeLabel = item.kind === 'attr' ? `@${item.attr}` : (item.tag || (item.kind === 'image' ? 'img' : 'texte'));
    return h('article', { class: classes.join(' '), dataset: { key: item.key } },
      h('div', { class: 'ck-studio__item-head', 'data-part': 'head' },
        h('div', { class: 'ck-studio__item-title' },
          h('span', { class: 'ck-studio__label' }, item.label),
          h('span', { class: 'ck-tag ck-tag--mono ck-studio__type' }, typeLabel),
          statusBadges(ctx, item)),
        h('div', { class: 'ck-studio__actions' }, rowActions(ctx, item))),
      fieldFor(ctx, item),
      h('div', { 'data-part': 'problem' }, problemBox(ctx, item)),
      metaLine(ctx, item) || h('div', { 'data-part': 'meta' }));
  }

  function findRow(key) {
    return Array.from(document.querySelectorAll('#studio-items [data-key]')).find((row) => row.dataset.key === key) || null;
  }

  /** Mise à jour en place : un champ en cours de saisie n'est jamais réécrit. */
  function syncRow(ctx, key) {
    const row = findRow(key);
    const item = ctx.model.item(key);
    if (!row || !item) return;
    const fresh = renderRow(ctx, item);
    const active = document.activeElement;
    if (!(active && row.contains(active) && active.matches('textarea, input'))) {
      row.replaceWith(fresh);
      return;
    }
    row.className = fresh.className;
    ['head', 'problem', 'meta'].forEach((part) => {
      const current = row.querySelector(`[data-part="${part}"]`);
      const next = fresh.querySelector(`[data-part="${part}"]`);
      if (current && next) current.replaceWith(next);
    });
  }

  function highlightRow(key, scroll) {
    document.querySelectorAll('#studio-items .ck-studio__item.is-selected').forEach((row) => row.classList.remove('is-selected'));
    const row = key ? findRow(key) : null;
    if (!row) return;
    row.classList.add('is-selected');
    if (scroll && !$('studio-panel').hidden) row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  function focusField(key) {
    const row = findRow(key);
    const field = row && row.querySelector('textarea, button');
    if (field) field.focus();
  }

  /* ----------------------------------------------------------------------
     Menu des pages
     ---------------------------------------------------------------------- */
  function renderPageMenu(ctx) {
    const menu = $('studio-page-menu');
    const dirtyByFile = new Map();
    ctx.model.pending.forEach((_value, key) => {
      const item = ctx.model.item(key);
      if (item) dirtyByFile.set(item.file, (dirtyByFile.get(item.file) || 0) + 1);
    });
    const pages = ctx.model.pages();
    const nodes = [h('span', { class: 'ck-menu__label' }, `Pages du site (${pages.length})`)];
    pages.forEach((file) => {
      const current = file === ctx.state.page;
      const dirty = dirtyByFile.get(file);
      nodes.push(h('button', {
        type: 'button', class: 'ck-menu__item', role: 'menuitemradio', 'aria-checked': String(current),
        on: { click: () => ctx.actions.navigate(file) }
      }, window.UI.icon(current ? 'check' : 'file-text'), h('span', { class: 'ck-studio__menu-name' }, ctx.model.pageTitle(file)),
      h('span', { class: 'ck-menu__meta' }, dirty ? `${dirty} modif.` : SM.displayPath(file))));
    });
    if (!pages.length) nodes.push(h('span', { class: 'ck-menu__empty' }, 'Aucune page indexée'));
    window.UI.replace(menu, nodes);
    const name = ctx.state.page ? ctx.model.pageTitle(ctx.state.page) : 'Page';
    $('studio-page-name').textContent = name;
    $('studio-page-btn').title = ctx.state.page ? `${name} — ${SM.displayPath(ctx.state.page)}` : 'Choisir une page';
  }

  /* ----------------------------------------------------------------------
     Dialogue : image
     ---------------------------------------------------------------------- */
  function sizeHint(request) {
    if (!request || !request.rendered || !request.rendered.width) return null;
    const ratio = Math.max(1, Math.min(2, Number(request.dpr) || 1)) >= 1.5 ? 2 : 1.5;
    const width = Math.round(request.rendered.width * ratio);
    const height = Math.round(request.rendered.height * ratio);
    const natural = request.natural && request.natural.width
      ? ` L'image actuelle mesure ${request.natural.width} × ${request.natural.height} px.` : '';
    return `Affichée en ${request.rendered.width} × ${request.rendered.height} px : prévoyez au moins ${width} × ${height} px pour un rendu net.${natural}`;
  }

  function openImageDialog(ctx, item, request) {
    const current = ctx.model.current(item.key);
    const altKey = request && request.altKey ? SM.keyOf(item.file, request.altKey)
      : SM.keyOf(item.file, `${item.xpath}/@alt`);
    const altItem = ctx.model.item(altKey);
    const preview = h('img', { class: 'ck-studio__preview', alt: 'Aperçu de l\'image choisie', src: ctx.assetUrl(current) });
    const file = h('input', { type: 'file', id: 'img-file', class: 'ck-sr-only', accept: IMAGE_TYPES.join(',') });
    const fileName = h('span', { class: 'ck-text-caption' }, 'JPG, PNG, WEBP, SVG ou GIF · 5 Mo maximum');
    const zone = h('label', { class: 'ck-dropzone ck-studio__drop', for: 'img-file', 'data-ck-dropzone': '' },
      h('span', { class: 'ck-dropzone__part' },
        h('span', { class: 'ck-dropzone__icon' }, window.UI.icon('upload')),
        h('span', { class: 'ck-dropzone__title' }, 'Glissez une image ou cliquez pour choisir'),
        fileName), file);
    const url = window.UI.input('url', { id: 'img-url', type: 'url', mono: true, placeholder: 'https://…',
      value: /^https?:/i.test(current) ? current : '' });
    const alt = altItem ? window.UI.textarea('alt', { id: 'img-alt', rows: 2, maxlength: 500, value: ctx.model.current(altKey) }) : null;
    const error = window.UI.alert({ tone: 'danger', inline: true, text: '', class: 'ck-form-error' });
    let objectUrl = null;
    const showFile = () => {
      const chosen = file.files && file.files[0];
      if (!chosen) return;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      objectUrl = URL.createObjectURL(chosen);
      preview.src = objectUrl;
      fileName.textContent = `${chosen.name} · ${window.UI.fmt.number(chosen.size / 1024)} Ko`;
    };
    file.addEventListener('change', showFile);
    zone.addEventListener('ck:files', showFile);
    url.addEventListener('input', () => { const safe = safeImageUrl(url.value); if (safe) preview.src = ctx.assetUrl(safe); });
    const submit = window.UI.btn('Utiliser cette image', { variant: 'primary', icon: 'image', type: 'submit', attrs: { form: 'form-image' } });
    const hint = sizeHint(request);
    const form = h('form', { class: 'ck-form ck-stack', id: 'form-image' },
      h('div', { class: 'ck-studio__preview-wrap' }, preview),
      hint ? h('p', { class: 'ck-help' }, hint) : null,
      zone,
      window.UI.field('…ou adresse d\'une image en ligne', url, { optional: true }),
      alt ? window.UI.field('Texte alternatif', alt, { help: 'Décrit l\'image aux personnes malvoyantes et aux moteurs de recherche.' }) : null,
      error);
    const dialog = window.UI.dialog({
      title: 'Remplacer l\'image', sub: `${ctx.model.pageTitle(item.file)} · ${SM.displayPath(item.file)}`, body: form,
      footer: [window.UI.btn('Annuler', { variant: 'ghost', attrs: { 'data-ck-close': '' } }), submit]
    });
    dialog.addEventListener('close', () => { if (objectUrl) URL.revokeObjectURL(objectUrl); });
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      error.classList.remove('is-visible');
      window.UI.busy(submit, true, 'Envoi…');
      try {
        let value = url.value.trim() || current;
        const chosen = file.files && file.files[0];
        if (chosen) {
          if (!IMAGE_TYPES.includes(chosen.type)) throw new Error('Format non pris en charge : JPG, PNG, WEBP, SVG ou GIF.');
          if (chosen.size > MAX_FILE_BYTES) throw new Error('Image trop lourde : 5 Mo maximum.');
          value = await ctx.actions.upload(chosen);
        }
        if (!safeImageUrl(value)) throw new Error('Choisissez un fichier ou une adresse https valide.');
        if (value !== current) ctx.actions.setValue(item.key, value, { source: 'dialog' });
        if (alt && alt.value !== ctx.model.current(altKey)) ctx.actions.setValue(altKey, alt.value, { source: 'dialog' });
        window.Cockpit.closeDialog(dialog);
      } catch (failure) {
        error.querySelector('.ck-alert__text').textContent = failure.message;
        error.classList.add('is-visible');
      } finally {
        window.UI.busy(submit, false);
      }
    });
  }

  /* ----------------------------------------------------------------------
     Dialogue : revue avant publication
     ---------------------------------------------------------------------- */
  function diffValue(ctx, item, value) {
    if (item.kind === 'image') return h('img', { class: 'ck-studio__diff-thumb', alt: '', src: ctx.assetUrl(value) });
    return h('span', null, value || '(vide)');
  }

  function openReviewDialog(ctx) {
    const body = h('div', { class: 'ck-studio__review' });
    const publish = window.UI.btn('Publier', { variant: 'primary', icon: 'rocket' });
    const discardAll = window.UI.btn('Tout annuler', { variant: 'ghost', icon: 'undo' });
    const render = () => {
      const keys = Array.from(new Set([...ctx.model.pending.keys(), ...ctx.model.problems.keys()]));
      const items = sortItems(ctx, keys.map((key) => ctx.model.item(key)).filter(Boolean));
      publish.disabled = !ctx.model.dirtyCount();
      discardAll.disabled = !ctx.model.dirtyCount();
      const count = ctx.model.dirtyCount();
      publish.replaceChildren(window.UI.icon('rocket'),
        h('span', null, count ? `Publier ${count} modification${count > 1 ? 's' : ''}` : 'Publier'));
      if (!items.length) {
        window.UI.replace(body, window.UI.empty({ title: 'Tout est publié', desc: 'Aucune modification en attente.', icon: 'check-circle', small: true }));
        return;
      }
      window.UI.replace(body, items.map((item) => h('article', { class: 'ck-studio__review-row' },
        h('div', { class: 'ck-spread' },
          h('div', { class: 'ck-studio__item-title' },
            h('span', { class: 'ck-studio__label' }, item.label),
            h('span', { class: 'ck-text-caption ck-mono' }, SM.displayPath(item.file))),
          ctx.model.isDirty(item.key)
            ? window.UI.btn('Annuler', { variant: 'ghost', size: 'xs', icon: 'undo', onClick: () => { ctx.actions.discard(item.key); render(); } })
            : null),
        h('div', { class: 'ck-studio__diff' },
          h('div', { class: 'ck-studio__diff-before' }, h('span', { class: 'ck-text-overline' }, 'Avant'), diffValue(ctx, item, item.value)),
          h('div', { class: 'ck-studio__diff-after' }, h('span', { class: 'ck-text-overline' }, 'Après'), diffValue(ctx, item, ctx.model.current(item.key)))),
        problemBox(ctx, item))));
    };
    const dialog = window.UI.dialog({
      title: 'Modifications à publier', size: 'lg', body, between: true,
      footer: [discardAll, h('div', { class: 'ck-cluster' }, window.UI.btn('Fermer', { variant: 'ghost', attrs: { 'data-ck-close': '' } }), publish)]
    });
    discardAll.addEventListener('click', async () => {
      const ok = await window.UI.confirm({ title: 'Tout annuler ?', message: 'Toutes les modifications non publiées seront perdues.', confirmLabel: 'Tout annuler', tone: 'danger' });
      if (ok) {
        ctx.actions.discardAll();
        render();
      }
    });
    publish.addEventListener('click', async () => {
      await ctx.actions.publish();
      render();
      if (!ctx.model.dirtyCount() && !ctx.model.problems.size) window.Cockpit.closeDialog(dialog);
    });
    ctx.reviewRender = render;
    dialog.addEventListener('close', () => { ctx.reviewRender = null; });
    render();
  }

  /* ----------------------------------------------------------------------
     Dialogue : raccourcis clavier
     ---------------------------------------------------------------------- */
  function openShortcutsDialog() {
    const rows = [
      ['Double-clic', 'Modifier un texte ou remplacer une image'],
      ['Clic', 'Sélectionner un élément (son contenu s\'affiche dans le panneau)'],
      ['Entrée', 'Valider le texte · modifier l\'élément sélectionné'],
      ['Échap', 'Annuler la saisie en cours'],
      ['Tab · Maj+Tab', 'Élément suivant · précédent'],
      ['Ctrl+clic', 'Suivre un lien de la vitrine'],
      ['Ctrl+S', 'Publier les modifications'],
      ['Ctrl+Z · Ctrl+Y', 'Annuler · rétablir']
    ];
    window.UI.dialog({
      title: 'Raccourcis du Studio', size: 'sm',
      body: h('dl', { class: 'ck-studio__shortcuts' }, rows.map(([keys, text]) => [
        h('dt', null, keys.split(' · ').map((key, index) => [index ? ' · ' : null, h('kbd', { class: 'ck-kbd' }, key)])),
        h('dd', null, text)
      ])),
      footer: [window.UI.btn('Fermer', { variant: 'primary', attrs: { 'data-ck-close': '' } })]
    });
  }

  window.StudioUI = {
    PAGE_SIZE, renderPanel, syncRow, highlightRow, focusField, renderPageMenu,
    openImageDialog, openReviewDialog, openShortcutsDialog, safeImageUrl
  };
})(window, document);
