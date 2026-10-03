/*!
 * Cockpit DS — charts.js
 * Graphiques SVG sans dépendance, conformes à 12-charts.css :
 * courbes (spline monotone), mini-séries, disponibilité journalière, jauges 270°,
 * anneau de répartition, classement horizontal. Infobulle flottante unique,
 * redessin au redimensionnement, jumeau tabulaire pour les lecteurs d'écran.
 * 29th studio · © 2026
 */
(function (window, document) {
  'use strict';

  const Cockpit = window.Cockpit || (window.Cockpit = {});
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const MONTHS = ['janv.', 'févr.', 'mars', 'avr.', 'mai', 'juin', 'juil.', 'août', 'sept.', 'oct.', 'nov.', 'déc.'];

  /* ----------------------------------------------------------------------
     Utilitaires
     ---------------------------------------------------------------------- */
  function svg(tag, attrs) {
    const node = document.createElementNS(SVG_NS, tag);
    Object.entries(attrs || {}).forEach(([key, value]) => {
      if (value != null) node.setAttribute(key, String(value));
    });
    return node;
  }

  function html(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }

  const seriesColor = (index) => `var(--ck-series-${(index % 8) + 1})`;
  const isNum = (value) => typeof value === 'number' && Number.isFinite(value);

  function formatNumber(value, digits) {
    if (!isNum(value)) return '—';
    const d = digits == null ? (Math.abs(value) < 10 && value % 1 !== 0 ? 1 : 0) : digits;
    return new Intl.NumberFormat('fr-FR', { minimumFractionDigits: d, maximumFractionDigits: d }).format(value);
  }

  function parseDate(value) {
    if (value instanceof Date) return value;
    const text = String(value || '');
    // « 2026-09-30 » : minuit local (sinon décalage d'un jour selon le fuseau)
    if (/^\d{4}-\d{2}-\d{2}$/.test(text)) {
      const [y, m, d] = text.split('-').map(Number);
      return new Date(y, m - 1, d);
    }
    return new Date(text);
  }

  function formatLabel(value, format, span) {
    if (format === 'date') {
      const date = parseDate(value);
      return Number.isNaN(date.getTime()) ? String(value) : `${date.getDate()} ${MONTHS[date.getMonth()]}`;
    }
    if (format === 'datetime' || format === 'time') {
      const date = parseDate(value);
      if (Number.isNaN(date.getTime())) return String(value);
      const hh = String(date.getHours()).padStart(2, '0');
      const mm = String(date.getMinutes()).padStart(2, '0');
      if (format === 'time' || (span || 0) <= 26 * 3600 * 1000) return `${hh}:${mm}`;
      return `${date.getDate()} ${MONTHS[date.getMonth()]} ${hh}h`;
    }
    return String(value);
  }

  /** Borne supérieure « ronde » : 1, 2, 2.5, 5 × 10^n */
  function niceMax(max) {
    if (!isNum(max) || max <= 0) return 1;
    const exp = Math.pow(10, Math.floor(Math.log10(max)));
    const unit = max / exp;
    const step = unit <= 1 ? 1 : unit <= 2 ? 2 : unit <= 2.5 ? 2.5 : unit <= 5 ? 5 : 10;
    return step * exp;
  }

  /** Spline monotone (Fritsch–Carlson) : lisse, sans dépasser les données. */
  function monotonePath(points) {
    const n = points.length;
    if (!n) return '';
    if (n === 1) return `M${points[0][0].toFixed(1)},${points[0][1].toFixed(1)}`;
    const dx = [];
    const slope = [];
    for (let i = 0; i < n - 1; i += 1) {
      dx[i] = points[i + 1][0] - points[i][0];
      slope[i] = dx[i] ? (points[i + 1][1] - points[i][1]) / dx[i] : 0;
    }
    const tangent = new Array(n);
    tangent[0] = slope[0];
    tangent[n - 1] = slope[n - 2];
    for (let i = 1; i < n - 1; i += 1) {
      tangent[i] = slope[i - 1] * slope[i] <= 0 ? 0 : (slope[i - 1] + slope[i]) / 2;
    }
    for (let i = 0; i < n - 1; i += 1) {
      if (slope[i] === 0) {
        tangent[i] = 0;
        tangent[i + 1] = 0;
        continue;
      }
      const a = tangent[i] / slope[i];
      const b = tangent[i + 1] / slope[i];
      const s = a * a + b * b;
      if (s > 9) {
        const t = 3 / Math.sqrt(s);
        tangent[i] = t * a * slope[i];
        tangent[i + 1] = t * b * slope[i];
      }
    }
    let d = `M${points[0][0].toFixed(1)},${points[0][1].toFixed(1)}`;
    for (let i = 0; i < n - 1; i += 1) {
      const [x0, y0] = points[i];
      const [x1, y1] = points[i + 1];
      const h = dx[i] / 3;
      d += `C${(x0 + h).toFixed(1)},${(y0 + tangent[i] * h).toFixed(1)},${(x1 - h).toFixed(1)},${(y1 - tangent[i + 1] * h).toFixed(1)},${x1.toFixed(1)},${y1.toFixed(1)}`;
    }
    return d;
  }

  /** Segments continus (les valeurs nulles coupent la courbe). */
  function segments(values, xAt, yAt) {
    const parts = [];
    let current = [];
    values.forEach((value, index) => {
      if (isNum(value)) current.push([xAt(index), yAt(value)]);
      else if (current.length) {
        parts.push(current);
        current = [];
      }
    });
    if (current.length) parts.push(current);
    return parts;
  }

  /* ----------------------------------------------------------------------
     Infobulle flottante unique
     ---------------------------------------------------------------------- */
  let tip = null;

  function tooltip() {
    if (!tip) {
      tip = html('div', 'ck-chart-tip ck-chart-tip--float');
      tip.setAttribute('role', 'presentation');
      document.body.appendChild(tip);
    }
    return tip;
  }

  function showTip(clientX, clientY, head, rows) {
    const node = tooltip();
    node.replaceChildren();
    node.appendChild(html('div', 'ck-chart-tip__head', head));
    rows.forEach((row) => {
      const line = html('div', 'ck-chart-tip__row');
      const key = html('span', 'ck-chart-tip__key');
      key.style.setProperty('--s', row.color);
      line.append(key, html('span', 'ck-chart-tip__value', row.value), html('span', 'ck-chart-tip__name', row.name));
      node.appendChild(line);
    });
    node.classList.add('is-visible');
    const rect = node.getBoundingClientRect();
    let left = clientX + 14;
    let top = clientY - rect.height - 12;
    if (left + rect.width > window.innerWidth - 8) left = clientX - rect.width - 14;
    if (top < 8) top = clientY + 16;
    node.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
  }

  function hideTip() {
    if (tip) tip.classList.remove('is-visible');
  }

  /* ----------------------------------------------------------------------
     Redessin au redimensionnement
     ---------------------------------------------------------------------- */
  const redraws = new WeakMap();
  const observer = 'ResizeObserver' in window
    ? new ResizeObserver((entries) => entries.forEach((entry) => {
      const fn = redraws.get(entry.target);
      if (fn) window.requestAnimationFrame(fn);
    }))
    : null;

  function watch(node, fn) {
    redraws.set(node, fn);
    if (observer) observer.observe(node);
  }

  /* ----------------------------------------------------------------------
     Courbes (1..n séries)
     config : { labels, labelFormat, series: [{ name, values, area, unit }], height,
                unit, digits, yMax, emptyText, ariaLabel }
     ---------------------------------------------------------------------- */
  function line(target, config) {
    const cfg = config || {};
    const labels = cfg.labels || [];
    const series = (cfg.series || []).map((s, index) => ({
      name: s.name || `Série ${index + 1}`,
      values: (s.values || []).map((v) => (v == null || v === '' ? null : Number(v))),
      color: s.color || seriesColor(index),
      area: s.area != null ? s.area : index === 0,
      unit: s.unit != null ? s.unit : (cfg.unit || ''),
      digits: s.digits != null ? s.digits : cfg.digits
    }));

    const figure = html('figure', 'ck-chart');
    figure.style.margin = '0';
    if (cfg.height) figure.style.setProperty('--ck-chart-h', `${cfg.height}px`);

    if (cfg.legend !== false && series.length) {
      const head = html('div', 'ck-chart__head');
      const legend = html('ul', 'ck-legend');
      series.forEach((s) => {
        const item = html('li', 'ck-legend__item');
        const key = html('span', 'ck-legend__key');
        key.style.setProperty('--s', s.color);
        item.append(key, html('span', 'ck-legend__name', s.name));
        legend.appendChild(item);
      });
      head.appendChild(legend);
      if (cfg.headExtra) head.appendChild(cfg.headExtra);
      figure.appendChild(head);
    }

    const plot = html('div', 'ck-chart__plot');
    plot.tabIndex = 0;
    plot.setAttribute('role', 'img');
    plot.setAttribute('aria-label', cfg.ariaLabel || series.map((s) => s.name).join(', '));
    figure.appendChild(plot);
    figure.appendChild(srTable(labels, series, cfg));

    const host = typeof target === 'string' ? document.querySelector(target) : target;
    host.replaceChildren(figure);

    const draw = () => drawLine(plot, labels, series, cfg);
    draw();
    watch(plot, draw);
    return figure;
  }

  function srTable(labels, series, cfg) {
    const table = html('table', 'ck-sr-only');
    const head = html('tr');
    head.appendChild(html('th', null, cfg.tableHeading || 'Période'));
    series.forEach((s) => head.appendChild(html('th', null, s.name)));
    table.appendChild(head);
    labels.forEach((label, index) => {
      const row = html('tr');
      row.appendChild(html('td', null, formatLabel(label, cfg.labelFormat)));
      series.forEach((s) => row.appendChild(html('td', null, formatNumber(s.values[index], s.digits))));
      table.appendChild(row);
    });
    return table;
  }

  function drawLine(plot, labels, series, cfg) {
    const width = Math.max(120, plot.clientWidth);
    const height = Math.max(80, plot.clientHeight);
    const pad = { top: 10, right: 12, bottom: 24, left: 44 };
    const innerW = width - pad.left - pad.right;
    const innerH = height - pad.top - pad.bottom;
    const n = Math.max(labels.length, ...series.map((s) => s.values.length), 1);

    const all = series.flatMap((s) => s.values.filter(isNum));
    const dataMax = all.length ? Math.max(...all) : 0;
    const yMax = cfg.yMax != null ? cfg.yMax : niceMax(dataMax * 1.08);
    const xAt = (i) => pad.left + (n === 1 ? innerW / 2 : (innerW * i) / (n - 1));
    const yAt = (v) => pad.top + innerH - (Math.max(0, Math.min(v, yMax)) / yMax) * innerH;

    const root = svg('svg', { viewBox: `0 0 ${width} ${height}`, width, height, 'aria-hidden': 'true' });

    // Grille horizontale + graduations Y (4 intervalles)
    for (let i = 0; i <= 4; i += 1) {
      const value = (yMax * i) / 4;
      const y = yAt(value);
      root.appendChild(svg('line', { class: i === 0 ? 'ck-chart__baseline' : 'ck-chart__grid', x1: pad.left, x2: width - pad.right, y1: y, y2: y }));
      const tick = svg('text', { class: 'ck-chart__tick', x: pad.left - 8, y: y + 3.5, 'text-anchor': 'end' });
      tick.textContent = formatNumber(value, yMax < 10 && value % 1 !== 0 ? 1 : 0);
      root.appendChild(tick);
    }

    // Graduations X : ~6 libellés répartis
    const span = labels.length > 1 ? parseDate(labels[labels.length - 1]) - parseDate(labels[0]) : 0;
    const every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(innerW / 90))));
    labels.forEach((label, i) => {
      if (i % every !== 0 && i !== n - 1) return;
      if (i !== n - 1 && n - 1 - i < every * 0.75) return;   // pas de chevauchement avec le dernier libellé
      const anchor = i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle';
      const text = svg('text', { class: 'ck-chart__tick', x: xAt(i), y: height - 6, 'text-anchor': anchor });
      text.textContent = formatLabel(label, cfg.labelFormat, span);
      root.appendChild(text);
    });

    // Aires puis lignes
    series.forEach((s) => {
      segments(s.values, xAt, yAt).forEach((points) => {
        const d = monotonePath(points);
        if (s.area && points.length > 1) {
          const last = points[points.length - 1];
          const area = svg('path', {
            class: 'ck-chart__area',
            d: `${d}L${last[0].toFixed(1)},${(pad.top + innerH).toFixed(1)}L${points[0][0].toFixed(1)},${(pad.top + innerH).toFixed(1)}Z`
          });
          area.style.setProperty('--s', s.color);
          root.appendChild(area);
        }
        const path = svg('path', { class: 'ck-chart__line', d });
        path.style.setProperty('--s', s.color);
        root.appendChild(path);
      });
      const lastIndex = s.values.map(isNum).lastIndexOf(true);
      if (lastIndex >= 0) {
        const dot = svg('circle', { class: 'ck-chart__dot', cx: xAt(lastIndex), cy: yAt(s.values[lastIndex]), r: 3.5 });
        dot.style.setProperty('--s', s.color);
        root.appendChild(dot);
      }
    });

    // Curseur + points survolés
    const cursor = svg('line', { class: 'ck-chart__cursor', y1: pad.top, y2: pad.top + innerH, x1: -10, x2: -10, visibility: 'hidden' });
    root.appendChild(cursor);
    const hoverDots = series.map((s) => {
      const dot = svg('circle', { class: 'ck-chart__dot', r: 4, cx: -10, cy: -10, visibility: 'hidden' });
      dot.style.setProperty('--s', s.color);
      root.appendChild(dot);
      return dot;
    });

    const hit = svg('rect', { class: 'ck-chart__hit', x: pad.left, y: pad.top, width: innerW, height: innerH });
    root.appendChild(hit);

    const empty = !all.length || dataMax === 0;
    plot.replaceChildren(root);
    if (empty && cfg.emptyText) plot.appendChild(html('div', 'ck-chart__empty', cfg.emptyText));

    function onMove(event) {
      const rect = root.getBoundingClientRect();
      const x = ((event.clientX - rect.left) / rect.width) * width;
      const index = Math.max(0, Math.min(n - 1, Math.round(((x - pad.left) / Math.max(1, innerW)) * (n - 1))));
      const cx = xAt(index);
      cursor.setAttribute('x1', cx);
      cursor.setAttribute('x2', cx);
      cursor.setAttribute('visibility', 'visible');
      const rows = [];
      series.forEach((s, i) => {
        const value = s.values[index];
        if (isNum(value)) {
          hoverDots[i].setAttribute('cx', cx);
          hoverDots[i].setAttribute('cy', yAt(value));
          hoverDots[i].setAttribute('visibility', 'visible');
        } else {
          hoverDots[i].setAttribute('visibility', 'hidden');
        }
        rows.push({ color: s.color, name: s.name, value: `${formatNumber(value, s.digits)}${isNum(value) && s.unit ? ` ${s.unit}` : ''}` });
      });
      showTip(event.clientX, event.clientY, formatLabel(labels[index], cfg.labelFormat === 'date' ? 'date' : 'datetime', 0), rows);
    }

    function onLeave() {
      cursor.setAttribute('visibility', 'hidden');
      hoverDots.forEach((dot) => dot.setAttribute('visibility', 'hidden'));
      hideTip();
    }

    hit.addEventListener('pointermove', onMove);
    hit.addEventListener('pointerleave', onLeave);
  }

  /* ----------------------------------------------------------------------
     Mini-série (sparkline) — série en retrait, dernier quart à l'accent
     ---------------------------------------------------------------------- */
  function spark(target, values, options) {
    const opts = options || {};
    const host = typeof target === 'string' ? document.querySelector(target) : target;
    const data = (values || []).map((v) => (isNum(Number(v)) && v != null ? Number(v) : null));
    const draw = () => {
      const width = Math.max(40, host.clientWidth || 120);
      const height = Math.max(16, host.clientHeight || 32);
      const nums = data.filter(isNum);
      const max = opts.max != null ? opts.max : Math.max(1, ...nums) * 1.1;
      const n = Math.max(2, data.length);
      const xAt = (i) => 2 + ((width - 4) * i) / (n - 1);
      const yAt = (v) => height - 2 - (Math.max(0, Math.min(v, max)) / max) * (height - 4);
      const root = svg('svg', { class: 'ck-spark', viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: 'none', 'aria-hidden': 'true' });
      segments(data, xAt, yAt).forEach((points) => {
        const d = monotonePath(points);
        if (points.length > 1) {
          root.appendChild(svg('path', { class: 'ck-spark__area', d: `${d}L${points[points.length - 1][0]},${height}L${points[0][0]},${height}Z` }));
        }
        root.appendChild(svg('path', { class: 'ck-spark__line', d }));
        const recent = points.slice(Math.floor(points.length * 0.72));
        if (recent.length > 1) root.appendChild(svg('path', { class: 'ck-spark__recent', d: monotonePath(recent) }));
      });
      const last = data.map(isNum).lastIndexOf(true);
      if (last >= 0) root.appendChild(svg('circle', { class: 'ck-spark__end', cx: xAt(last), cy: yAt(data[last]), r: 2.5 }));
      host.replaceChildren(root);
    };
    draw();
    watch(host, draw);
    return host;
  }

  /* ----------------------------------------------------------------------
     Disponibilité journalière (90 barres) — état = statut
     days : [{ date, state: up|degraded|down|none, uptime }]
     ---------------------------------------------------------------------- */
  const STATE_LABEL = { up: 'Opérationnel', degraded: 'Dégradé', down: 'Interruption', none: 'Aucune donnée' };

  function uptime(target, days, options) {
    const opts = options || {};
    const host = typeof target === 'string' ? document.querySelector(target) : target;
    const wrap = html('div', 'ck-uptime');
    const bars = html('div', 'ck-uptime__bars');
    if (opts.height) bars.style.height = `${opts.height}px`;
    (days || []).forEach((day) => {
      const tick = html('span', 'ck-uptime__tick');
      const state = day.state || 'none';
      tick.dataset.state = state;
      const date = formatLabel(day.date, 'date');
      tick.title = isNum(day.uptime)
        ? `${date} · ${formatNumber(day.uptime, 2)} % · ${STATE_LABEL[state] || state}`
        : `${date} · ${STATE_LABEL.none}`;
      bars.appendChild(tick);
    });
    const total = (days || []).length;
    bars.setAttribute('role', 'img');
    bars.setAttribute('aria-label', opts.ariaLabel || `Disponibilité quotidienne sur ${total} jours`);
    wrap.appendChild(bars);
    if (opts.axis !== false && total) {
      const axis = html('div', 'ck-uptime__axis');
      axis.append(html('span', null, `Il y a ${total} jours`), html('span', null, "Aujourd'hui"));
      wrap.appendChild(axis);
    }
    host.replaceChildren(wrap);
    return wrap;
  }

  /* ----------------------------------------------------------------------
     Jauge circulaire 270° — { value (0-100), label, size, warn, danger, text }
     ---------------------------------------------------------------------- */
  function gauge(target, config) {
    const cfg = config || {};
    const host = typeof target === 'string' ? document.querySelector(target) : target;
    const size = cfg.size || 88;
    const stroke = Math.max(6, Math.round(size / 11));
    const r = (size - stroke) / 2;
    const c = 2 * Math.PI * r;
    const arc = c * 0.75;
    const value = isNum(cfg.value) ? Math.max(0, Math.min(100, cfg.value)) : null;
    const tone = value == null ? 'neutral'
      : value >= (cfg.danger || 90) ? 'danger' : value >= (cfg.warn || 75) ? 'warn' : (cfg.tone || 'accent');
    const text = cfg.text != null ? cfg.text : (value == null ? '—' : `${Math.round(value)} %`);

    // Mise à jour en place (jauges temps réel) : l'arc glisse vers la nouvelle valeur, sans clignoter.
    const existing = host.firstElementChild;
    if (existing && existing.classList.contains('ck-gauge') && existing.dataset.size === String(size)) {
      if (tone !== 'accent') existing.dataset.tone = tone;
      else delete existing.dataset.tone;
      if (value != null) existing.setAttribute('aria-valuenow', String(Math.round(value)));
      existing.querySelector('.ck-gauge__arc').setAttribute('stroke-dashoffset', value == null ? arc : arc * (1 - value / 100));
      existing.querySelector('.ck-gauge__value').textContent = text;
      return existing;
    }

    const node = html('div', 'ck-gauge');
    node.dataset.size = String(size);
    node.style.setProperty('--size', `${size}px`);
    if (tone !== 'accent') node.dataset.tone = tone;
    node.setAttribute('role', 'meter');
    node.setAttribute('aria-label', cfg.label || 'Jauge');
    node.setAttribute('aria-valuemin', '0');
    node.setAttribute('aria-valuemax', '100');
    if (value != null) node.setAttribute('aria-valuenow', String(Math.round(value)));

    const root = svg('svg', { viewBox: `0 0 ${size} ${size}`, 'aria-hidden': 'true' });
    const center = size / 2;
    const rotate = `rotate(135 ${center} ${center})`;
    root.appendChild(svg('circle', { class: 'ck-gauge__track', cx: center, cy: center, r, 'stroke-width': stroke, 'stroke-dasharray': `${arc} ${c}`, transform: rotate, 'stroke-linecap': 'round' }));
    const offset = value == null ? arc : arc * (1 - value / 100);
    const fill = svg('circle', { class: 'ck-gauge__arc', cx: center, cy: center, r, 'stroke-width': stroke, 'stroke-dasharray': `${arc} ${c}`, 'stroke-dashoffset': offset, transform: rotate });
    fill.style.setProperty('--arc', String(arc));
    root.appendChild(fill);
    node.appendChild(root);
    node.appendChild(html('span', 'ck-gauge__value', text));
    host.replaceChildren(node);
    return node;
  }

  /* ----------------------------------------------------------------------
     Anneau de répartition — { items: [{ label, value, color }], size, value, label }
     ---------------------------------------------------------------------- */
  function donut(target, config) {
    const cfg = config || {};
    const host = typeof target === 'string' ? document.querySelector(target) : target;
    const items = (cfg.items || []).filter((item) => isNum(item.value) && item.value > 0);
    const size = cfg.size || 132;
    const stroke = Math.round(size / 9);
    const r = (size - stroke) / 2;
    const c = 2 * Math.PI * r;
    const total = items.reduce((sum, item) => sum + item.value, 0);
    const gap = items.length > 1 ? 2 : 0;

    const node = html('div', 'ck-donut');
    node.style.setProperty('--size', `${size}px`);
    const root = svg('svg', { viewBox: `0 0 ${size} ${size}`, 'aria-hidden': 'true' });
    const center = size / 2;
    root.appendChild(svg('circle', { class: 'ck-donut__track', cx: center, cy: center, r, 'stroke-width': stroke }));
    let offset = 0;
    items.forEach((item, index) => {
      const length = total ? (item.value / total) * c : 0;
      const seg = svg('circle', {
        class: 'ck-donut__seg', cx: center, cy: center, r, 'stroke-width': stroke,
        'stroke-dasharray': `${Math.max(0, length - gap)} ${c}`, 'stroke-dashoffset': -offset,
        transform: `rotate(-90 ${center} ${center})`
      });
      seg.style.setProperty('--s', item.color || seriesColor(index));
      const title = svg('title');
      title.textContent = `${item.label} : ${formatNumber(item.value, cfg.digits)}${cfg.unit ? ` ${cfg.unit}` : ''}`;
      seg.appendChild(title);
      root.appendChild(seg);
      offset += length;
    });
    node.appendChild(root);
    const middle = html('div', 'ck-donut__center');
    middle.append(html('span', 'ck-donut__value', cfg.value != null ? cfg.value : formatNumber(total, cfg.digits)),
      html('span', 'ck-donut__label', cfg.label || ''));
    node.appendChild(middle);
    node.setAttribute('role', 'img');
    node.setAttribute('aria-label', items.map((item) => `${item.label} ${formatNumber(item.value, cfg.digits)}`).join(', '));
    host.replaceChildren(node);
    return node;
  }

  /* ----------------------------------------------------------------------
     Classement horizontal — items : [{ label, value, display, color }]
     ---------------------------------------------------------------------- */
  function barlist(target, items, options) {
    const opts = options || {};
    const host = typeof target === 'string' ? document.querySelector(target) : target;
    const list = html('ul', 'ck-barlist');
    const max = opts.max || Math.max(1, ...(items || []).map((item) => Number(item.value) || 0));
    (items || []).forEach((item, index) => {
      const row = html('li', 'ck-barlist__item');
      const track = html('span', 'ck-barlist__track');
      const bar = html('span', 'ck-barlist__bar');
      bar.style.setProperty('--s', item.color || seriesColor(opts.color != null ? opts.color : 0));
      track.appendChild(bar);
      row.append(html('span', 'ck-barlist__label', item.label),
        html('span', 'ck-barlist__value', item.display != null ? item.display : formatNumber(item.value)), track);
      bar.style.setProperty('--v', `${Math.max(1, ((Number(item.value) || 0) / max) * 100)}%`);
      bar.style.animationDelay = `${index * 40}ms`;
      list.appendChild(row);
    });
    host.replaceChildren(list);
    return list;
  }

  /* ----------------------------------------------------------------------
     Montage déclaratif : <div data-ck-chart="line" data-ck-config='{…}'>
     ---------------------------------------------------------------------- */
  const KINDS = {
    line: (el, cfg) => line(el, cfg),
    area: (el, cfg) => line(el, cfg),
    spark: (el, cfg) => spark(el, cfg.values, cfg),
    uptime: (el, cfg) => uptime(el, cfg.days, cfg),
    gauge: (el, cfg) => gauge(el, cfg),
    donut: (el, cfg) => donut(el, cfg),
    barlist: (el, cfg) => barlist(el, cfg.items, cfg)
  };

  function mount(scope) {
    const root = scope && scope.querySelectorAll ? scope : document;
    root.querySelectorAll('[data-ck-chart]').forEach((el) => {
      const render = KINDS[el.getAttribute('data-ck-chart')];
      if (!render) return;
      try {
        render(el, JSON.parse(el.getAttribute('data-ck-config') || '{}'));
      } catch (error) {
        el.textContent = '';
      }
    });
  }

  document.addEventListener('scroll', hideTip, true);
  Cockpit.charts = { line, spark, uptime, gauge, donut, barlist, mount, formatLabel, niceMax };
})(window, document);
