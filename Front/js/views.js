/*!
 * Cockpit WaaS — views.js
 * Rendus métier partagés par plusieurs pages (une seule copie) : tableaux des
 * sites, factures et échanges, reçu imprimable, carte de disponibilité 90 jours,
 * messagerie (liste + fil + composeur), graphique de fréquentation.
 * 29th studio · © 2026
 */
(function (window) {
  'use strict';

  const { h, fmt } = window.UI;
  const UI = window.UI;
  const Cockpit = window.Cockpit;

  /* ----------------------------------------------------------------------
     Sites
     ---------------------------------------------------------------------- */
  function planCell(site) {
    const price = Number(site.price_monthly) || 0;
    return h('td', null,
      UI.tag(site.plan_name || 'Sans formule', { tone: price >= 80 ? 'accent' : null }),
      h('span', { class: 'ck-table__sub ck-mono' }, price > 0 ? `${fmt.number(price)} €/mois` : 'Inclus'));
  }

  function siteDomain(site) {
    if (site.domain) return site.domain;
    if (site.url) return String(site.url).replace(/^https?:\/\//, '').replace(/\/$/, '');
    return site.port ? `port ${site.port}` : 'non publié';
  }

  /** Aide du champ « Dépôt Git » (déploiement et fiche) selon le jeton GitHub de l'API. */
  function repoHelp(privateRepos) {
    return privateRepos
      ? 'Dépôt https public, ou GitHub privé : lu par l\'API avec le jeton de la plateforme, jamais transmis à la vitrine. Accès vérifié à l\'enregistrement. Tout le dépôt est publié : il ne doit contenir que les fichiers du site.'
      : 'Dépôt https public. Pour un dépôt GitHub privé, définissez WAAS_GITHUB_TOKEN dans le fichier .env de l\'API (statut visible dans Paramètres).';
  }

  /**
   * Tableau des sites. mode 'vps' : bouton VPS seul (tableau de bord) ;
   * par défaut : Studio + Visiter + VPS (catalogue).
   */
  function sitesTable(sites, options) {
    const opts = options || {};
    const mode = opts.mode || 'default';
    const rows = sites.map((site) => {
      const detail = `/admin/sites/detail?id=${encodeURIComponent(site.site_id)}`;
      const vps = mode === 'vps'
        ? UI.btn('VPS', { href: `/admin/vps/detail?id=${encodeURIComponent(site.site_id)}`, icon: 'server', size: 'xs', title: `Serveur de ${site.name}` })
        : UI.btn(`Serveur de ${site.name}`, { href: `/admin/vps/detail?id=${encodeURIComponent(site.site_id)}`, icon: 'server', variant: 'ghost', size: 'xs', iconOnly: true });
      const actions = mode === 'vps' ? [vps] : [
        UI.btn('Studio', { href: `/studio?site=${encodeURIComponent(site.site_id)}`, icon: 'pencil', variant: 'primary', size: 'xs' }),
        site.url ? UI.btn(`Visiter ${site.name}`, { href: site.url, icon: 'external', variant: 'ghost', size: 'xs', iconOnly: true, external: true }) : null,
        vps
      ];
      return UI.clickableRow(detail, [
        UI.tdPrimary(site.name, siteDomain(site), { href: detail }),
        UI.td(site.client_name ? h('span', { class: 'ck-table__primary', title: site.client_name }, site.client_name) : h('span', { class: 'ck-muted' }, 'Non assigné')),
        planCell(site),
        UI.td(UI.statusBadge(site.status)),
        UI.tdNum(UI.live(site.live_visitors, { small: true, suffix: '' })),
        UI.tdActions(...actions)
      ]);
    });
    if (opts.viewAll && sites.length) rows.push(UI.viewAll(opts.viewAll.label, opts.viewAll.href, 6));
    if (!sites.length) {
      return UI.empty({
        title: opts.emptyTitle || 'Aucune vitrine enregistrée',
        desc: opts.emptyDesc || "Déployez votre premier site client : l'éditeur visuel et l'hébergement sont activés en quelques secondes.",
        icon: 'globe',
        actions: opts.emptyAction === false ? null : [UI.btn('Déployer un site', { href: '/admin/sites?new=1', icon: 'plus', variant: 'primary', size: 'sm' })]
      });
    }
    return UI.table({
      caption: 'Sites et vitrines',
      columns: ['Site web', 'Client', 'Formule', 'Statut', { label: 'En direct', kind: 'num' }, { label: mode === 'vps' ? 'Serveur' : 'Actions', kind: 'actions' }],
      rows
    });
  }

  /* ----------------------------------------------------------------------
     Support : priorité, tableau des échanges
     ---------------------------------------------------------------------- */
  function priorityBadge(priority) {
    if (priority === 'urgent') return UI.badge('Urgent', 'danger', { size: 'sm' });
    return UI.tag({ low: 'Basse', normal: 'Normale' }[priority] || 'Normale');
  }

  function ticketsTable(tickets, options) {
    const opts = options || {};
    const hrefFor = opts.hrefFor || ((t) => `/admin/support?ticket=${encodeURIComponent(t.id)}`);
    if (!tickets.length) {
      return UI.empty({ title: 'Aucun échange en cours', desc: 'Les demandes de modification, retours clients et messages de support apparaîtront ici.', icon: 'message' });
    }
    const rows = tickets.map((t) => UI.clickableRow(hrefFor(t), [
      UI.tdPrimary(t.subject, `${t.site_name} — ${t.snippet || 'Aucun message'}`, { href: hrefFor(t), mono: false }),
      UI.td(UI.statusBadge(t.status)),
      UI.td(priorityBadge(t.priority)),
      UI.td(fmt.day(t.updated_at || t.created_at)),
      UI.tdActions(UI.btn('Répondre', { href: hrefFor(t), size: 'xs' }))
    ]));
    if (opts.viewAll) rows.push(UI.viewAll(opts.viewAll.label, opts.viewAll.href, 5));
    return UI.table({ caption: 'Tickets de support', columns: ['Sujet & dernier message', 'Statut', 'Priorité', 'Date', { label: '', kind: 'actions' }], rows });
  }

  /* ----------------------------------------------------------------------
     Facturation
     ---------------------------------------------------------------------- */
  function invoicesTable(invoices, options) {
    const opts = options || {};
    if (!invoices.length) {
      return UI.empty({ title: opts.emptyTitle || 'Aucune facture émise', desc: opts.emptyDesc || 'Le suivi de la facturation et des abonnements récurrents apparaîtra ici.', icon: 'credit-card', actions: opts.emptyActions });
    }
    const columns = ['Facture', 'Date', opts.showSite === false ? 'Prestation' : 'Site & client', { label: 'Montant', kind: 'num' }, 'Statut', { label: '', kind: 'actions' }];
    const rows = invoices.map((inv) => h('tr', null,
      UI.td(inv.number, 'ck-mono'),
      UI.td(fmt.date(inv.issued_at)),
      opts.showSite === false
        ? UI.td(inv.description || 'Abonnement vitrine & hébergement')
        : UI.tdPrimary(inv.site_name || inv.site_id || '—', inv.client_name || null, { mono: false }),
      UI.tdNum(h('strong', { class: 'ck-text-strong' }, fmt.money(inv.amount))),
      UI.td(UI.statusBadge(inv.status)),
      UI.tdActions(...(opts.actions ? opts.actions(inv) : []))));
    return UI.table({ caption: 'Factures', columns, rows });
  }

  /** Actions d'une facture côté agence : reçu, encaissement (et suppression d'un brouillon). */
  function adminInvoiceActions(inv, onChange) {
    const receipt = UI.btn(inv.status === 'paid' ? 'Reçu' : 'Voir', {
      icon: 'file-text', variant: 'ghost', size: 'xs',
      onClick: async () => {
        try { receiptDialog(await window.App.api(`/admin/api/invoices/${encodeURIComponent(inv.id)}`)); } catch (error) { window.App.toastError(error); }
      }
    });
    if (inv.status === 'paid') return [receipt];
    const pay = UI.btn('Marquer payée', {
      icon: 'check', size: 'xs',
      onClick: async (event) => {
        const button = event.currentTarget;
        UI.busy(button, true);
        try {
          await window.App.api(`/admin/api/invoices/${encodeURIComponent(inv.id)}/pay`, { method: 'POST' });
          window.App.toastOk('Facture encaissée', `${inv.number} · ${fmt.money(inv.amount)}`);
          if (onChange) await onChange();
        } catch (error) {
          window.App.toastError(error);
          UI.busy(button, false);
        }
      }
    });
    const remove = inv.status === 'draft' ? UI.btn(`Supprimer ${inv.number}`, {
      icon: 'trash', variant: 'ghost', size: 'xs', iconOnly: true,
      onClick: async () => {
        const ok = await UI.confirm({ title: 'Supprimer le brouillon ?', message: `${inv.number} sera définitivement supprimée.`, confirmLabel: 'Supprimer', tone: 'danger', icon: 'trash' });
        if (!ok) return;
        try {
          await window.App.api(`/admin/api/invoices/${encodeURIComponent(inv.id)}`, { method: 'DELETE' });
          if (onChange) await onChange();
        } catch (error) { window.App.toastError(error); }
      }
    }) : null;
    return [receipt, pay, remove];
  }

  /** Dialogue « Nouvelle facture » (sites : [{ site_id, name, price_monthly }]). */
  function invoiceDialog(options) {
    const opts = options || {};
    const sites = opts.sites || [];
    const site = UI.select('site_id', sites.map((s) => [s.site_id, s.name]), { id: 'inv-site', value: opts.siteId, required: true });
    const amount = UI.input('amount', { id: 'inv-amount', type: 'number', min: 0, step: '0.01', mono: true, required: true });
    const description = UI.input('description', { id: 'inv-desc', required: true, maxlength: 200, value: 'Abonnement vitrine & hébergement' });
    const status = UI.select('status', [['pending', 'En attente de règlement'], ['draft', 'Brouillon'], ['paid', 'Déjà réglée']], { id: 'inv-status' });
    const due = UI.input('due_at', { id: 'inv-due', type: 'date' });
    const errorBox = UI.alert({ tone: 'danger', inline: true, text: '', class: 'ck-form-error' });
    const syncAmount = () => {
      const chosen = sites.find((s) => s.site_id === site.value);
      if (chosen && !amount.dataset.touched) amount.value = chosen.price_monthly || 0;
    };
    amount.addEventListener('input', () => { amount.dataset.touched = '1'; });
    site.addEventListener('change', syncAmount);
    syncAmount();
    const submit = UI.btn('Émettre la facture', { variant: 'primary', icon: 'file-text', type: 'submit', attrs: { form: 'form-invoice' } });
    const form = h('form', { class: 'ck-form', id: 'form-invoice' },
      h('div', { class: 'ck-form-grid' },
        UI.field('Vitrine', site, { required: true }),
        UI.field('Montant (€)', amount, { required: true }),
        UI.field('Prestation', description, { required: true, full: true }),
        UI.field('Statut', status),
        UI.field('Échéance', due, { optional: true, help: 'Par défaut : 14 jours après émission.' })),
      errorBox);
    const dialog = UI.dialog({
      title: 'Nouvelle facture', sub: 'Numérotation automatique FAC-AAAA-NNNN.', body: form,
      footer: [UI.btn('Annuler', { variant: 'ghost', attrs: { 'data-ck-close': '' } }), submit]
    });
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!form.reportValidity()) return;
      const values = UI.formValues(form);
      if (!values.due_at) delete values.due_at;
      UI.busy(submit, true, 'Émission…');
      try {
        const result = await window.App.api('/admin/api/invoices', { method: 'POST', body: values });
        Cockpit.closeDialog(dialog);
        window.App.toastOk('Facture émise', `${result.invoice.number} · ${fmt.money(result.invoice.amount)}`);
        if (opts.onCreated) await opts.onCreated(result.invoice);
      } catch (error) {
        errorBox.querySelector('.ck-alert__text').textContent = error.message;
        errorBox.classList.add('is-visible');
      } finally {
        UI.busy(submit, false);
      }
    });
    return dialog;
  }

  /** Reçu imprimable (payload : { invoice, site, issuer }). */
  function receiptDialog(payload) {
    const inv = payload.invoice || {};
    const issuer = payload.issuer || {};
    const site = payload.site || {};
    const paid = inv.status === 'paid';
    const items = Array.isArray(inv.items) && inv.items.length
      ? inv.items
      : [{ description: inv.description || 'Abonnement vitrine & hébergement', quantity: 1, unit_price: inv.amount, total: inv.amount }];
    const doc = h('div', { class: 'ck-receipt' },
      h('div', { class: 'ck-receipt__parties' },
        h('div', { class: 'ck-receipt__party' },
          h('span', { class: 'ck-receipt__role' }, 'Émetteur'),
          h('span', { class: 'ck-receipt__name' }, issuer.name || '29th studio'),
          issuer.address ? h('span', { class: 'ck-receipt__line' }, issuer.address) : null,
          issuer.email ? h('span', { class: 'ck-receipt__line ck-mono' }, issuer.email) : null,
          issuer.vat ? h('span', { class: 'ck-receipt__line ck-mono' }, issuer.vat) : null),
        h('div', { class: 'ck-receipt__party' },
          h('span', { class: 'ck-receipt__role' }, 'Client'),
          h('span', { class: 'ck-receipt__name' }, inv.client_name || 'Client'),
          h('span', { class: 'ck-receipt__line' }, site.name || inv.site_id || 'Vitrine'),
          site.domain ? h('span', { class: 'ck-receipt__line ck-mono' }, site.domain) : null)),
      h('div', { class: 'ck-table-wrap' },
        h('table', { class: 'ck-table ck-table--compact' },
          h('thead', null, h('tr', null,
            h('th', { scope: 'col' }, 'Désignation'), h('th', { scope: 'col', class: 'is-num' }, 'Qté'),
            h('th', { scope: 'col', class: 'is-num' }, 'Prix unitaire'), h('th', { scope: 'col', class: 'is-num' }, 'Total'))),
          h('tbody', null, items.map((item) => h('tr', null,
            h('td', null, item.description || '—'),
            h('td', { class: 'is-num' }, String(item.quantity || 1)),
            h('td', { class: 'is-num' }, fmt.money(item.unit_price != null ? item.unit_price : item.total)),
            h('td', { class: 'is-num' }, fmt.money(item.total))))))),
      h('dl', { class: 'ck-receipt__totals' },
        h('dt', null, 'Émise le'), h('dd', null, fmt.date(inv.issued_at)),
        h('dt', null, 'Échéance'), h('dd', null, fmt.date(inv.due_at)),
        h('dt', { class: 'is-total' }, paid ? 'Total réglé' : 'Total à régler'), h('dd', { class: 'is-total' }, fmt.money(inv.amount))));
    const printBtn = UI.btn('Imprimer / PDF', { icon: 'printer', size: 'sm', onClick: () => window.App.printNode(doc) });
    return UI.dialog({
      title: `${paid ? 'Reçu de paiement' : 'Facture'} · ${inv.number}`,
      sub: paid ? `Acquittée le ${fmt.date(inv.paid_at)}` : `Émise le ${fmt.date(inv.issued_at)}`,
      rawBody: true,
      body: h('div', { class: 'ck-dialog__body' }, doc),
      between: true,
      footer: [
        UI.badge(paid ? 'Règlement acquitté' : (inv.status === 'overdue' ? 'Échéance dépassée' : 'En attente de règlement'), paid ? 'good' : (inv.status === 'overdue' ? 'danger' : 'warn')),
        h('div', { class: 'ck-cluster' }, printBtn, UI.btn('Fermer', { variant: 'ghost', size: 'sm', attrs: { 'data-ck-close': '' } }))
      ]
    });
  }

  /* ----------------------------------------------------------------------
     Fréquentation (courbe pages vues / visiteurs uniques)
     ---------------------------------------------------------------------- */
  function trafficChart(target, series, options) {
    const opts = options || {};
    const points = series || [];
    return Cockpit.charts.line(target, {
      labels: points.map((p) => p.date),
      labelFormat: 'date',
      tableHeading: 'Jour',
      height: opts.height || 220,
      ariaLabel: opts.ariaLabel || 'Fréquentation : pages vues et visiteurs uniques par jour',
      emptyText: opts.emptyText || 'Aucune visite enregistrée — le suivi est prêt.',
      series: [
        { name: 'Pages vues', values: points.map((p) => p.pageviews), digits: 0 },
        { name: 'Visiteurs uniques', values: points.map((p) => p.visitors), digits: 0, area: false }
      ]
    });
  }

  /* ----------------------------------------------------------------------
     Disponibilité 90 jours (vitrine ou service)
     ---------------------------------------------------------------------- */
  function uptimeLegend(note) {
    return h('div', { class: 'ck-spread', style: { 'margin-top': 'var(--ck-space-8)', 'flex-wrap': 'wrap' } },
      h('div', { class: 'ck-cluster', style: { gap: 'var(--ck-space-12)' } },
        UI.badge('Opérationnel (100 %)', 'good'), UI.badge('Dégradé (≥ 99 %)', 'warn'),
        UI.badge('Interruption (< 99 %)', 'danger'), UI.badge('Non mesuré', 'neutral')),
      note ? h('span', { class: 'ck-text-caption ck-mono' }, note) : null);
  }

  function availabilityBody(data) {
    const measured = Number(data.applicable) > 0;
    const uptimeText = measured ? fmt.percent(data.uptime, 2) : '—';
    const tone = !measured || data.uptime >= 99.9 ? 'good' : data.uptime >= 99 ? 'warn' : 'danger';
    const bars = h('div');
    Cockpit.charts.uptime(bars, data.series || [], { ariaLabel: 'Disponibilité quotidienne sur 90 jours' });
    return h('div', { class: 'ck-stack', style: { '--ck-stack-gap': 'var(--ck-space-16)' } },
      UI.stats([
        ['État courant', UI.statusBadge(data.current_state || 'none')],
        ['Disponibilité 90 j', uptimeText],
        ['Latence moyenne', data.avg_latency_ms != null ? `${fmt.decimal(data.avg_latency_ms)} ms` : '—'],
        ['Contrôles', `${fmt.number(data.applicable)} mesure${data.applicable > 1 ? 's' : ''}`],
        ['Dernière lecture', data.current_code || '—']
      ]),
      h('div', { style: { 'padding-top': 'var(--ck-space-16)', 'border-top': 'var(--ck-border)' } },
        h('div', { class: 'ck-spread', style: { 'margin-bottom': 'var(--ck-space-8)' } },
          h('span', { class: 'ck-text-caption' }, 'Historique de disponibilité — 90 derniers jours'),
          UI.tag(`${uptimeText} · ${fmt.number(data.applicable)} contrôle(s)`, { mono: true, tone })),
        bars,
        uptimeLegend(`${fmt.number(data.down)} échec(s) · ${fmt.number(data.degraded)} dégradé(s) · ${fmt.number(data.skipped)} hors mesure`)));
  }

  /* ----------------------------------------------------------------------
     Messagerie : liste des conversations, fil, composeur
     ---------------------------------------------------------------------- */
  function conversationList(options) {
    const opts = options || {};
    const tickets = opts.tickets || [];
    const items = tickets.map((t) => {
      const link = h('a', {
        class: 'ck-conv', href: opts.hrefFor(t), 'aria-current': t.id === opts.selectedId ? 'page' : null,
        on: opts.onSelect ? { click: (event) => { event.preventDefault(); opts.onSelect(t); } } : null
      },
      h('span', { class: 'ck-conv__row' }, h('span', { class: 'ck-conv__subject' }, t.subject), UI.statusBadge(t.status, { size: 'sm' })),
      h('span', { class: 'ck-conv__row' },
        h('span', { class: 'ck-conv__meta' }, opts.metaFor ? opts.metaFor(t) : t.site_name),
        t.priority === 'urgent' ? priorityBadge('urgent') : null,
        t.unread ? UI.count(t.unread, 'accent') : null,
        h('span', { class: 'ck-conv__date' }, fmt.day(t.updated_at || t.created_at))),
      h('span', { class: 'ck-conv__snippet' }, t.snippet || 'Aucun message'));
      return link;
    });
    const actions = [];
    if (opts.filter) {
      const make = (value, label) => h('button', {
        type: 'button', class: `ck-btn ck-btn--secondary ck-btn--xs${opts.filter.value === value ? ' is-active' : ''}`,
        'aria-pressed': String(opts.filter.value === value), on: { click: () => opts.filter.onChange(value) }
      }, label);
      actions.push(h('div', { class: 'ck-btn-group', role: 'group', 'aria-label': 'Filtrer les conversations' }, make('open', 'Ouverts'), make('all', 'Tous')));
    }
    if (opts.newAction) actions.push(opts.newAction);
    const body = items.length
      ? h('div', { class: 'ck-conv-list' }, items)
      : UI.empty({ title: 'Aucune conversation', desc: opts.emptyText || 'Aucune conversation pour le moment.', icon: 'message', small: true });
    return h('article', { class: 'ck-card' }, UI.cardHeader(h('span', null, opts.title || 'Conversations', ' ', UI.count(tickets.length)), null, actions), body);
  }

  function threadStream(ticket) {
    const nodes = [];
    let currentDay = null;
    (ticket.messages || []).forEach((msg) => {
      const day = fmt.day(msg.sent_at);
      if (day !== currentDay) {
        nodes.push(h('span', { class: 'ck-thread__day' }, day));
        currentDay = day;
      }
      const agency = msg.from_role === 'agency';
      nodes.push(h('div', { class: `ck-msg ck-msg--${agency ? 'agency' : 'client'}` },
        h('div', { class: 'ck-msg__meta' },
          h('span', { class: 'ck-msg__author' }, agency ? `Studio · ${msg.author_name || '29th studio'}` : (msg.author_name || 'Client')),
          h('span', null, fmt.time(msg.sent_at))),
        h('p', { class: 'ck-msg__bubble' }, msg.body)));
    });
    if (!nodes.length) nodes.push(UI.empty({ title: 'Aucun message', desc: 'Écrivez le premier message ci-dessous.', icon: 'message', small: true }));
    const stream = h('div', { class: 'ck-thread__stream', 'aria-live': 'polite' }, nodes);
    window.requestAnimationFrame(() => { stream.scrollTop = stream.scrollHeight; });
    return stream;
  }

  function threadCard(ticket, options) {
    const opts = options || {};
    const closed = ticket.status === 'closed';
    const toggle = UI.btn(closed ? (opts.reopenLabel || 'Rouvrir') : (opts.resolveLabel || 'Fermer le ticket'), {
      icon: closed ? 'undo' : 'check', size: 'xs',
      onClick: (event) => opts.onToggle && opts.onToggle(closed ? 'open' : 'closed', event.currentTarget)
    });
    const field = UI.textarea('body', { id: 'support-reply', rows: 3, placeholder: opts.placeholder || 'Écrivez votre réponse…', required: true, maxlength: 5000 });
    const send = UI.btn('Envoyer', { icon: 'send', variant: 'primary', size: 'sm', type: 'submit' });
    const form = h('form', { class: 'ck-composer' },
      h('label', { for: 'support-reply', class: 'ck-sr-only' }, 'Votre réponse'),
      field,
      h('div', { class: 'ck-composer__foot' }, h('span', null, opts.hint || ''), send));
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const body = field.value.trim();
      if (!body || !opts.onReply) return;
      UI.busy(send, true, 'Envoi…');
      try {
        await opts.onReply(body);
        field.value = '';
      } finally {
        UI.busy(send, false);
      }
    });
    field.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) form.requestSubmit();
    });
    const badges = h('span', { class: 'ck-cluster' }, UI.statusBadge(ticket.status, { size: 'sm' }),
      ticket.priority === 'urgent' ? priorityBadge('urgent') : null, h('span', null, opts.subtitle || ''));
    return h('article', { class: 'ck-card ck-thread' }, UI.cardHeader(ticket.subject, badges, [toggle]), threadStream(ticket), form);
  }

  function emptyThread(title, text) {
    return h('article', { class: 'ck-card ck-thread' }, UI.cardHeader('Discussion'), UI.empty({ title, desc: text, icon: 'message' }));
  }

  /* ----------------------------------------------------------------------
     Console : lignes colorées (commande, sortie, erreur, système)
     ---------------------------------------------------------------------- */
  function consoleLine(text, kind) {
    return h('div', { class: `ck-console__line${kind ? ` is-${kind}` : ''}` }, text);
  }

  function logLines(lines) {
    return (lines || []).map((line) => {
      const lower = String(line).toLowerCase();
      const kind = /error|erreur|fatal|failed|échec|exception/.test(lower) ? 'err'
        : /warn|avert/.test(lower) ? 'warn'
          : /\[done\]|ready|prêt|succès|success|démarrage de nginx/.test(lower) ? 'ok' : null;
      return consoleLine(line, kind);
    });
  }

  window.Views = {
    planCell, siteDomain, repoHelp, sitesTable, priorityBadge, ticketsTable, invoicesTable, adminInvoiceActions, invoiceDialog, receiptDialog,
    trafficChart, uptimeLegend, availabilityBody, conversationList, threadStream, threadCard, emptyThread,
    consoleLine, logLines
  };
})(window);
