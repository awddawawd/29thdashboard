/*!
 * Cockpit DS — ui.js
 * Briques de rendu DOM (port de OldDashBoard/components/ui.py) : chaque fonction
 * produit exactement le balisage de la librairie (classes ck-*). Les données ne
 * passent jamais par innerHTML : textContent et attributs uniquement.
 * 29th studio · © 2026
 */
(function (window, document) {
  'use strict';

  const Cockpit = window.Cockpit;

  /* ----------------------------------------------------------------------
     Élément générique : h('div', { class, dataset, on, style, … }, ...enfants)
     ---------------------------------------------------------------------- */
  function h(tag, attrs, ...children) {
    const node = document.createElement(tag);
    const options = attrs || {};
    Object.entries(options).forEach(([key, value]) => {
      if (value == null || value === false) return;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = String(value);
      else if (key === 'dataset') Object.entries(value).forEach(([k, v]) => { if (v != null) node.dataset[k] = String(v); });
      else if (key === 'style' && typeof value === 'object') Object.entries(value).forEach(([k, v]) => node.style.setProperty(k, v));
      else if (key === 'on') Object.entries(value).forEach(([event, handler]) => node.addEventListener(event, handler));
      else if (key === 'href') node.setAttribute('href', Cockpit.safeHref(value));
      else if (value === true) node.setAttribute(key, '');
      else node.setAttribute(key, String(value));
    });
    append(node, children);
    return node;
  }

  function append(node, children) {
    [].concat(children).flat(Infinity).forEach((child) => {
      if (child == null || child === false || child === '') return;
      node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
    });
    return node;
  }

  function replace(node, ...children) {
    const target = typeof node === 'string' ? document.querySelector(node) : node;
    if (!target) return null;
    target.replaceChildren();
    return append(target, children);
  }

  /** Icône par référence au sprite (#i-…), injecté par cockpit.js. */
  function icon(name, className) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    node.setAttribute('class', `ck-icon${className ? ` ${className}` : ''}`);
    node.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', `#i-${name}`);
    node.appendChild(use);
    return node;
  }

  /* ----------------------------------------------------------------------
     Formats fr-FR
     ---------------------------------------------------------------------- */
  const MONTHS = ['janv.', 'févr.', 'mars', 'avr.', 'mai', 'juin', 'juil.', 'août', 'sept.', 'oct.', 'nov.', 'déc.'];
  const isNum = (value) => value != null && value !== '' && Number.isFinite(Number(value));

  function toDate(value) {
    if (!value) return null;
    if (value instanceof Date) return value;
    const text = String(value);
    if (/^\d{4}-\d{2}-\d{2}$/.test(text)) {
      const [y, m, d] = text.split('-').map(Number);
      return new Date(y, m - 1, d);
    }
    const date = new Date(text);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  const fmt = {
    number(value, digits) {
      return isNum(value) ? Cockpit.fmt.number(Number(value), digits || 0) : '—';
    },
    decimal(value, digits) {
      return isNum(value) ? Cockpit.fmt.number(Number(value), digits == null ? 1 : digits) : '—';
    },
    money(value, digits) {
      return isNum(value) ? Cockpit.fmt.currency(Number(value), digits == null ? 2 : digits) : '—';
    },
    percent(value, digits) {
      return isNum(value) ? `${Cockpit.fmt.number(Number(value), digits == null ? 1 : digits)} %` : '—';
    },
    mb(value) {
      if (!isNum(value)) return '—';
      const mb = Number(value);
      return mb >= 1024 ? `${Cockpit.fmt.number(mb / 1024, 2)} Go` : `${Cockpit.fmt.number(mb, 1)} Mo`;
    },
    date(value) {
      const date = toDate(value);
      return date ? `${date.getDate()} ${MONTHS[date.getMonth()]} ${date.getFullYear()}` : '—';
    },
    dateShort(value) {
      const date = toDate(value);
      return date ? `${date.getDate()} ${MONTHS[date.getMonth()]}` : '—';
    },
    time(value) {
      const date = toDate(value);
      return date ? date.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' }) : '';
    },
    dateTime(value) {
      const date = toDate(value);
      return date ? `${fmt.date(date)} · ${fmt.time(date)}` : '—';
    },
    relative(value) {
      const date = toDate(value);
      if (!date) return '—';
      const seconds = Math.round((Date.now() - date.getTime()) / 1000);
      if (seconds < 45) return "à l'instant";
      if (seconds < 3600) return `il y a ${Math.round(seconds / 60)} min`;
      if (seconds < 86400) return `il y a ${Math.round(seconds / 3600)} h`;
      const days = Math.round(seconds / 86400);
      if (days === 1) return 'hier';
      if (days < 7) return `il y a ${days} j`;
      return fmt.date(date);
    },
    day(value) {
      const date = toDate(value);
      if (!date) return '';
      const today = new Date();
      const diff = Math.round((new Date(today.getFullYear(), today.getMonth(), today.getDate())
        - new Date(date.getFullYear(), date.getMonth(), date.getDate())) / 86400000);
      if (diff === 0) return "Aujourd'hui";
      if (diff === 1) return 'Hier';
      return fmt.date(date);
    },
    duration(seconds) {
      return isNum(seconds) ? Cockpit.fmt.duration(Number(seconds)) : '—';
    },
    delay(days) {
      if (days == null) return 'Non datée';
      if (days === 0) return "Aujourd'hui";
      return days < 0 ? `Depuis ${-days} j` : `Dans ${days} j`;
    }
  };

  function initials(name) {
    const parts = String(name || '').split(/[\s._@-]+/).filter(Boolean);
    return (parts.slice(0, 2).map((p) => p[0]).join('') || '?').toUpperCase();
  }

  /* ----------------------------------------------------------------------
     Statuts (sites, conteneurs, factures, tickets, sondes)
     ---------------------------------------------------------------------- */
  const STATUS = {
    running: ['good', 'En ligne'], online: ['good', 'En ligne'], active: ['good', 'Actif'], up: ['good', 'Opérationnel'],
    paid: ['good', 'Payée'],
    pending: ['warn', 'En attente'], waiting: ['warn', 'En attente'], degraded: ['warn', 'Dégradé'],
    paused: ['warn', 'En pause'], restarting: ['danger', 'Crashloop'],
    open: ['info', 'Ouvert'], starting: ['info', 'Démarrage…'], stopping: ['info', 'Arrêt…'],
    provisioning: ['info', 'Provisionnement…'],
    not_provisioned: ['warn', 'Sans serveur'],
    stopped: ['neutral', 'Arrêté'], down: ['danger', 'Interruption'], overdue: ['danger', 'En retard'],
    error: ['danger', 'Erreur'],
    closed: ['neutral', 'Résolu'], draft: ['neutral', 'Brouillon'], cancelled: ['neutral', 'Résilié'],
    skipped: ['neutral', 'Non mesuré'], none: ['neutral', 'Non mesuré'], unknown: ['neutral', 'Inconnu']
  };
  const SPINNING = new Set(['starting', 'stopping', 'provisioning']);

  const statusTone = (status) => (STATUS[String(status || '').toLowerCase()] || ['neutral'])[0];
  const statusLabel = (status) => (STATUS[String(status || '').toLowerCase()] || [null, String(status || 'Inconnu')])[1];

  /* ----------------------------------------------------------------------
     Primitives d'affichage
     ---------------------------------------------------------------------- */
  function badge(label, tone, options) {
    const opts = options || {};
    return h('span', {
      class: `ck-badge${opts.size ? ` ck-badge--${opts.size}` : ''}${opts.spin ? ' ck-badge--spin' : ''}`,
      'data-tone': tone || 'neutral',
      title: opts.title
    }, opts.dot === false ? null : h('span', { class: 'ck-badge__dot' }), label);
  }

  function statusBadge(status, options) {
    const key = String(status || '').toLowerCase();
    return badge(statusLabel(key), statusTone(key), { ...(options || {}), spin: SPINNING.has(key) });
  }

  function tag(label, options) {
    const opts = options || {};
    return h('span', {
      class: `ck-tag${opts.mono ? ' ck-tag--mono' : ''}`,
      'data-tone': opts.tone,
      title: opts.title
    }, opts.icon ? icon(opts.icon) : null, label);
  }

  function count(value, tone) {
    return h('span', { class: 'ck-count', 'data-tone': tone }, String(value));
  }

  function live(value, options) {
    const opts = options || {};
    const n = Number(value) || 0;
    return h('span', { class: `ck-live${n <= 0 ? ' is-idle' : ''}${opts.small ? ' ck-live--sm' : ''}`, title: opts.title },
      h('span', { class: 'ck-live__dot' }), opts.suffix === '' ? String(n) : `${n} ${opts.suffix || 'en direct'}`);
  }

  function btn(label, options) {
    const opts = options || {};
    const classes = ['ck-btn', `ck-btn--${opts.variant || 'secondary'}`];
    if (opts.size) classes.push(`ck-btn--${opts.size}`);
    if (opts.iconOnly) classes.push('ck-btn--icon');
    if (opts.block) classes.push('ck-btn--block');
    if (opts.class) classes.push(opts.class);
    const attrs = { class: classes.join(' '), ...(opts.attrs || {}) };
    if (opts.iconOnly) {
      attrs['aria-label'] = label;
      attrs.title = opts.title || label;
    } else if (opts.title) {
      attrs.title = opts.title;
    }
    if (opts.onClick) attrs.on = { click: opts.onClick };
    const children = [
      opts.icon ? icon(opts.icon) : null,
      opts.iconOnly ? null : (opts.icon || opts.iconAfter ? h('span', null, label) : label),
      opts.iconAfter ? icon(opts.iconAfter) : null
    ];
    if (opts.href != null) {
      attrs.href = opts.href;
      if (opts.external) {
        attrs.target = '_blank';
        attrs.rel = 'noopener noreferrer';
      }
      return h('a', attrs, children);
    }
    attrs.type = opts.type || 'button';
    if (opts.disabled) attrs.disabled = true;
    return h('button', attrs, children);
  }

  /** Bouton occupé : désactivé + spinner, libellé restauré ensuite. */
  function busy(button, isBusy, label) {
    if (!button) return;
    if (isBusy) {
      button.dataset.label = button.dataset.label || button.innerHTML;
      button.disabled = true;
      button.classList.add('is-loading');
      button.setAttribute('aria-busy', 'true');
      if (label) {
        button.replaceChildren(h('span', { class: 'ck-spinner', 'aria-hidden': 'true' }), h('span', null, label));
      }
    } else {
      button.disabled = false;
      button.classList.remove('is-loading');
      button.removeAttribute('aria-busy');
      if (button.dataset.label != null) {
        button.innerHTML = button.dataset.label;       // balisage d'origine (contrôlé par la page)
        delete button.dataset.label;
      }
    }
  }

  function kpi(options) {
    const opts = options || {};
    return h('article', { class: `ck-card ck-kpi${opts.hero ? ' ck-kpi--hero' : ''}`, 'data-tone': opts.tone || 'neutral', id: opts.id },
      h('div', { class: 'ck-kpi__head' },
        h('span', { class: 'ck-kpi__label' }, opts.label),
        opts.icon ? icon(opts.icon, 'ck-kpi__icon') : null),
      h('div', { class: 'ck-kpi__value' }, opts.value, opts.unit ? h('span', { class: 'ck-kpi__unit' }, opts.unit) : null),
      opts.meta || opts.trend ? h('div', { class: 'ck-kpi__foot' }, opts.trend || null,
        opts.meta ? h('span', { class: 'ck-kpi__meta' }, opts.meta) : null) : null,
      opts.extra || null);
  }

  function kpiRow(...items) {
    return h('div', { class: 'ck-kpi-row' }, items);
  }

  function delta(text, good) {
    const up = !String(text).trim().startsWith('-') && !String(text).trim().startsWith('−');
    return h('span', { class: 'ck-delta', 'data-tone': (good == null ? up : good) ? 'good' : 'danger' },
      icon(up ? 'arrow-up-right' : 'arrow-down-right'), text);
  }

  function cardHeader(title, subtitle, actions) {
    return h('header', { class: 'ck-card__header' },
      h('div', { class: 'ck-card__heading' },
        h('h2', { class: 'ck-card__title' }, title),
        subtitle ? h('p', { class: 'ck-card__subtitle' }, subtitle) : null),
      actions && [].concat(actions).filter(Boolean).length ? h('div', { class: 'ck-card__actions' }, actions) : null);
  }

  function card(options) {
    const opts = options || {};
    const body = opts.padded === false ? opts.body : h('div', { class: `ck-card__body${opts.bodyClass ? ` ${opts.bodyClass}` : ''}` }, opts.body);
    return h('article', { class: `ck-card${opts.class ? ` ${opts.class}` : ''}`, id: opts.id },
      opts.title != null ? cardHeader(opts.title, opts.subtitle, opts.actions) : null,
      body,
      opts.footer || null);
  }

  function cardFooter(text, link) {
    return h('footer', { class: 'ck-card__footer' },
      h('span', null, text || ''),
      link ? h('a', { class: 'ck-link ck-link--arrow', href: link.href }, link.label, icon('arrow-right')) : null);
  }

  function stats(items) {
    return h('div', { class: 'ck-stats' }, items.filter(Boolean).map(([label, value, mono]) =>
      h('div', { class: 'ck-stat' },
        h('span', { class: 'ck-stat__label' }, label),
        h('span', { class: `ck-stat__value${mono === false || value instanceof Node ? '' : ' ck-mono'}` }, value))));
  }

  function dl(items) {
    return h('dl', { class: 'ck-dl' }, items.filter(Boolean).map(([term, desc]) => [h('dt', null, term), h('dd', null, desc)]));
  }

  function empty(options) {
    const opts = options || {};
    return h('div', { class: `ck-empty${opts.small ? ' ck-empty--sm' : ''}` },
      h('div', { class: 'ck-empty__icon' }, icon(opts.icon || 'inbox')),
      h('p', { class: 'ck-empty__title' }, opts.title),
      opts.desc ? h('p', { class: 'ck-empty__desc' }, opts.desc) : null,
      opts.actions ? h('div', { class: 'ck-empty__actions' }, opts.actions) : null);
  }

  const ALERT_ICONS = { good: 'check-circle', info: 'info', accent: 'info', warn: 'alert-triangle', danger: 'alert-circle', neutral: 'info' };

  function alert(options) {
    const opts = options || {};
    const tone = opts.tone || 'info';
    return h('div', {
      class: `ck-alert${opts.bar ? ' ck-alert--bar' : ''}${opts.inline ? ' ck-alert--inline' : ''}${opts.class ? ` ${opts.class}` : ''}`,
      'data-tone': tone,
      role: tone === 'danger' ? 'alert' : 'status',
      id: opts.id
    },
    icon(opts.icon || ALERT_ICONS[tone] || 'info', 'ck-alert__icon'),
    h('div', { class: 'ck-alert__body' },
      opts.title ? h('p', { class: 'ck-alert__title' }, opts.title) : null,
      h('p', { class: 'ck-alert__text' }, opts.text)),
    opts.actions ? h('div', { class: 'ck-alert__actions' }, opts.actions) : null);
  }

  function progress(percent, tone, label) {
    const pct = Math.max(0, Math.min(100, Number(percent) || 0));
    const level = tone || (pct >= 90 ? 'danger' : pct >= 75 ? 'warn' : 'accent');
    return h('div', {
      class: 'ck-progress', 'data-tone': level, role: 'progressbar', 'aria-label': label || 'Progression',
      'aria-valuenow': Math.round(pct), 'aria-valuemin': 0, 'aria-valuemax': 100
    }, h('div', { class: 'ck-progress__bar', style: { '--v': `${pct.toFixed(1)}%` } }));
  }

  /* ----------------------------------------------------------------------
     Tableaux
     ---------------------------------------------------------------------- */
  function table(options) {
    const opts = options || {};
    const head = h('tr', null, (opts.columns || []).map((col) => {
      const column = typeof col === 'string' ? { label: col } : col;
      return h('th', { scope: 'col', class: column.kind === 'num' ? 'is-num' : column.kind === 'actions' ? 'is-actions' : null }, column.label);
    }));
    const rows = opts.rows && opts.rows.length ? opts.rows : [
      h('tr', null, h('td', { class: 'is-empty', colspan: (opts.columns || []).length || 1 }, opts.emptyText || 'Aucune donnée.'))
    ];
    return h('div', { class: 'ck-table-wrap' },
      h('table', { class: `ck-table${opts.compact ? ' ck-table--compact' : ''}`, 'aria-label': opts.caption },
        h('thead', null, head), h('tbody', { id: opts.bodyId }, rows)));
  }

  function td(content, className) {
    return h('td', { class: className }, content);
  }

  function tdPrimary(title, sub, options) {
    const opts = options || {};
    return h('td', null,
      opts.href ? h('a', { class: 'ck-table__primary', href: opts.href, title }, title) : h('span', { class: 'ck-table__primary', title }, title),
      sub ? h('span', { class: `ck-table__sub${opts.mono === false ? '' : ' ck-mono'}`, title: sub }, sub) : null);
  }

  function tdNum(...content) {
    return h('td', { class: 'is-num' }, content);
  }

  function tdActions(...actions) {
    return h('td', { class: 'is-actions ck-table__actions' }, actions);
  }

  function viewAll(label, href, colspan) {
    return h('tr', { class: 'ck-table__viewall' }, h('td', { colspan }, h('a', { href }, label, icon('arrow-right'))));
  }

  /** Ligne cliquable : un clic hors contrôle interactif ouvre ``href``. */
  function clickableRow(href, cells) {
    const row = h('tr', { class: 'is-clickable', dataset: { href } }, cells);
    return row;
  }

  document.addEventListener('click', (event) => {
    const row = event.target instanceof Element && event.target.closest('tr[data-href]');
    if (!row || event.target.closest('a, button, input, select, textarea, label, form')) return;
    const href = Cockpit.safeHref(row.dataset.href);
    if (href === '#') return;
    if (event.metaKey || event.ctrlKey) window.open(href, '_blank', 'noopener');
    else window.location.href = href;
  });

  /* ----------------------------------------------------------------------
     Formulaires
     ---------------------------------------------------------------------- */
  function field(label, control, options) {
    const opts = options || {};
    const id = control.id || opts.id;
    return h('div', { class: `ck-field${opts.full ? ' is-full' : ''}` },
      h('label', { class: 'ck-label', for: id }, label,
        opts.required ? h('span', { class: 'ck-label__req', 'aria-hidden': 'true' }, '*') : null,
        opts.optional ? h('span', { class: 'ck-label__opt' }, 'optionnel') : null),
      control,
      opts.help ? h('p', { class: 'ck-help' }, opts.help) : null);
  }

  function input(name, options) {
    const opts = options || {};
    return h('input', {
      class: `ck-input${opts.mono ? ' ck-input--mono' : ''}${opts.size ? ` ck-input--${opts.size}` : ''}`,
      type: opts.type || 'text', name, id: opts.id || `f-${name}`, value: opts.value, placeholder: opts.placeholder,
      required: opts.required, pattern: opts.pattern, min: opts.min, max: opts.max, step: opts.step,
      minlength: opts.minlength, maxlength: opts.maxlength, autocomplete: opts.autocomplete || 'off',
      inputmode: opts.inputmode, readonly: opts.readonly
    });
  }

  function select(name, choices, options) {
    const opts = options || {};
    const node = h('select', { class: `ck-select${opts.size ? ` ck-select--${opts.size}` : ''}`, name, id: opts.id || `f-${name}`, required: opts.required },
      choices.map(([value, label]) => h('option', { value }, label)));
    if (opts.value != null) node.value = String(opts.value);
    return node;
  }

  function textarea(name, options) {
    const opts = options || {};
    const node = h('textarea', {
      class: 'ck-textarea', name, id: opts.id || `f-${name}`, rows: opts.rows || 4, placeholder: opts.placeholder,
      required: opts.required, maxlength: opts.maxlength
    });
    if (opts.value) node.value = opts.value;
    return node;
  }

  /** Données d'un formulaire : chaînes nettoyées, cases à cocher en booléens. */
  function formValues(form) {
    const values = {};
    Array.from(form.elements).forEach((element) => {
      if (!element.name || element.disabled) return;
      if (element.type === 'checkbox') values[element.name] = element.checked;
      else if (element.type === 'number') values[element.name] = element.value === '' ? null : Number(element.value);
      else values[element.name] = typeof element.value === 'string' ? element.value.trim() : element.value;
    });
    return values;
  }

  /* ----------------------------------------------------------------------
     Dialogues : construits à la demande dans #modal-container, supprimés à la fermeture
     ---------------------------------------------------------------------- */
  let dialogSeq = 0;

  function dialog(options) {
    const opts = options || {};
    dialogSeq += 1;
    const id = opts.id || `dlg-${dialogSeq}`;
    const titleId = `${id}-t`;
    const node = h('dialog', {
      class: `ck-dialog${opts.size ? ` ck-dialog--${opts.size}` : ''}`,
      id, 'aria-labelledby': titleId, 'data-ck-dispose': opts.keep ? null : true
    },
    h('header', { class: 'ck-dialog__header' },
      h('div', null,
        h('h2', { class: 'ck-dialog__title', id: titleId }, opts.title),
        opts.sub ? h('p', { class: `ck-dialog__sub${opts.subMono ? ' ck-mono' : ''}` }, opts.sub) : null),
      btn('Fermer', { variant: 'ghost', size: 'sm', icon: 'x', iconOnly: true, class: 'ck-dialog__close', attrs: { 'data-ck-close': '' } })),
    opts.rawBody ? opts.body : h('div', { class: 'ck-dialog__body ck-stack' }, opts.body),
    opts.footer ? h('footer', { class: `ck-dialog__footer${opts.between ? ' ck-dialog__footer--between' : ''}` }, opts.footer) : null);
    const root = document.getElementById('modal-container') || document.body;
    root.appendChild(node);
    if (opts.open !== false) Cockpit.openDialog(node);
    return node;
  }

  /** Confirmation accessible (remplace window.confirm) : Promise<boolean>. */
  function confirm(options) {
    const opts = options || {};
    return new Promise((resolve) => {
      let answered = false;
      const ok = btn(opts.confirmLabel || 'Confirmer', { variant: opts.tone === 'danger' ? 'danger' : 'primary', class: opts.tone === 'danger' ? 'ck-btn--solid' : '', icon: opts.icon });
      const cancel = btn('Annuler', { variant: 'ghost', attrs: { 'data-ck-close': '' } });
      const node = dialog({
        title: opts.title || 'Confirmer',
        size: 'sm',
        body: [h('p', { class: 'ck-text-body' }, opts.message || ''), opts.extra || null],
        footer: [cancel, ok]
      });
      ok.addEventListener('click', () => {
        answered = true;
        Cockpit.closeDialog(node);
        resolve(true);
      });
      node.addEventListener('close', () => { if (!answered) resolve(false); });
    });
  }

  window.UI = {
    h, append, replace, icon, fmt, initials, isNum, toDate,
    STATUS, statusTone, statusLabel, badge, statusBadge, tag, count, live, btn, busy,
    kpi, kpiRow, delta, card, cardHeader, cardFooter, stats, dl, empty, alert, progress,
    table, td, tdPrimary, tdNum, tdActions, viewAll, clickableRow,
    field, input, select, textarea, formValues, dialog, confirm
  };
})(window, document);
