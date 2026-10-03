/*!
 * Studio — modèle (logique pure, sans DOM)
 * Éléments éditables identifiés par (page, XPath), modifications en attente,
 * annuler / rétablir (frappe regroupée), application des résultats du serveur
 * (enregistré, conflit, refusé). Exposé sous window.StudioModel.
 * 29th studio · © 2026
 */
(function (window) {
  'use strict';

  const SEP = '\u0000';
  const ORPHAN_PREFIX = 'orphan:';
  const COALESCE_MS = 1200;
  const KINDS = new Set(['text', 'image', 'attr']);

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
    alt: 'Texte alternatif', placeholder: "Texte d'aide du champ", value: 'Bouton de formulaire',
    content: 'Description (Google)'
  };
  /** Longueurs conseillées (au-delà, Google tronque l'affichage). */
  const SEO_LIMITS = { title: 60, description: 160 };

  const keyOf = (file, xpath) => `${file}${SEP}${xpath}`;
  const norm = (value) => String(value == null ? '' : value).replace(/[ \t\r\n]+/g, ' ').replace(/^ +| +$/g, '');

  /** XPath -> { tag, attr } : « /html/body/p[2]/text()[normalize-space()][3] » -> { tag: 'p' }. */
  function describeXPath(xpath) {
    let path = String(xpath || '');
    if (path.indexOf(ORPHAN_PREFIX) === 0) path = path.replace(/^orphan:[^:]*:/, '');
    let attr = null;
    const attrMatch = /\/@([A-Za-z_][\w.-]*)$/.exec(path);
    if (attrMatch) {
      attr = attrMatch[1].toLowerCase();
      path = path.slice(0, attrMatch.index);
    }
    path = path.replace(/\/text\(\)(\[[^\]]*\])*$/, '');
    const last = path.split('/').pop() || '';
    const tag = last.replace(/\[.*$/, '').toLowerCase();
    return { tag: /^[a-z][a-z0-9-]*$/.test(tag) ? tag : '', attr };
  }

  /**
   * Page -> adresse publique : « about/index.html » -> « /about/ ». ``null`` pour un chemin
   * qui sortirait de la vitrine (« /hôte/… » donnerait « //hôte/… », « .. » remonterait).
   */
  function pagePath(file) {
    const raw = String(file || '');
    const parts = raw.split('/');
    if (!raw || parts.some((part) => !part || part === '.' || part === '..') || /[\\\u0000-\u001f]/.test(raw)) {
      return null;
    }
    const encoded = parts.map(encodeURIComponent).join('/');
    if (/(^|\/)index\.html?$/i.test(raw)) return `/${encoded.replace(/index\.html?$/i, '')}`;
    return `/${encoded}`;
  }

  /** Chemin affichable (jamais utilisé pour naviguer). */
  const displayPath = (file) => pagePath(file) || String(file || '');

  function comparePages(a, b) {
    const rank = (file) => (/^index\.html?$/i.test(file) ? 0 : 1);
    return rank(a) - rank(b) || a.localeCompare(b, 'fr', { numeric: true });
  }

  function fromRow(row) {
    const xpath = String(row.xpath || '');
    const file = String(row.file_path || '');
    const detached = xpath.indexOf(ORPHAN_PREFIX) === 0;
    const { tag, attr } = describeXPath(xpath);
    const kind = KINDS.has(row.content_type) ? row.content_type : 'text';
    const item = {
      key: keyOf(file, xpath), id: row.id, file, xpath, kind, tag, attr, detached,
      value: row.content_value == null ? '' : String(row.content_value),
      source: row.source_value == null ? null : String(row.source_value),
      updatedAt: row.updated_at || null, updatedBy: row.updated_by || null
    };
    item.label = labelOf(item);
    item.seo = (tag === 'title' && kind === 'text') || (tag === 'meta' && attr === 'content');
    return item;
  }

  function labelOf(item) {
    if (item.kind === 'image') return 'Image';
    if (item.kind === 'attr') return ATTR_LABELS[item.attr] || 'Attribut';
    const base = TAG_LABELS[item.tag] || 'Texte';
    return /^h[1-6]$/.test(item.tag) ? `${base} · ${item.tag.toUpperCase()}` : base;
  }

  function seoLimit(item) {
    if (item.tag === 'title') return SEO_LIMITS.title;
    if (item.tag === 'meta' && item.attr === 'content') return SEO_LIMITS.description;
    return 0;
  }

  /* ----------------------------------------------------------------------
     État
     ---------------------------------------------------------------------- */
  function createModel() {
    const model = {
      items: [], byKey: new Map(), pending: new Map(), problems: new Map(),
      undo: [], redo: [], anchors: false, audit: false
    };

    model.load = function load(payload) {
      const previous = model.byKey;
      model.items = (payload.content || []).map(fromRow);
      model.byKey = new Map(model.items.map((item) => [item.key, item]));
      model.anchors = Boolean(payload.anchors);
      model.audit = Boolean(payload.audit);
      // Les modifications non enregistrées survivent à un rechargement du contenu, avec
      // leur valeur de référence : un changement fait entre-temps reste un conflit.
      Array.from(model.pending.keys()).forEach((key) => {
        const item = model.byKey.get(key);
        if (!item) model.pending.delete(key);
        else if (previous.has(key)) item.value = previous.get(key).value;
      });
    };

    model.item = (key) => model.byKey.get(key) || null;
    model.current = (key) => {
      if (model.pending.has(key)) return model.pending.get(key);
      const item = model.byKey.get(key);
      return item ? item.value : '';
    };
    model.isDirty = (key) => model.pending.has(key);
    model.problem = (key) => model.problems.get(key) || null;
    model.dirtyCount = () => model.pending.size;

    model.pages = function pages() {
      const files = new Set(model.items.filter((item) => !item.detached && pagePath(item.file)).map((item) => item.file));
      return Array.from(files).sort(comparePages);
    };

    model.pageTitle = function pageTitle(file) {
      const title = model.items.find((item) => item.file === file && item.tag === 'title' && item.kind === 'text');
      const text = title ? norm(model.current(title.key)) : '';
      return text || pagePath(file) || file;
    };

    /**
     * Nouvelle valeur pour un élément. ``coalesce`` regroupe la frappe continue
     * d'un même champ en une seule étape d'annulation.
     */
    model.setValue = function setValue(key, value, options) {
      const opts = options || {};
      const item = model.byKey.get(key);
      if (!item) return null;
      const from = model.current(key);
      if (from === value) return null;
      if (value === item.value) model.pending.delete(key);
      else model.pending.set(key, value);
      model.problems.delete(key);
      if (opts.record !== false) {
        const last = model.undo[model.undo.length - 1];
        const now = Date.now();
        if (opts.coalesce && last && last.key === key && last.coalesce && now - last.at < COALESCE_MS) {
          last.to = value;
          last.at = now;
        } else {
          model.undo.push({ key, from, to: value, at: now, coalesce: Boolean(opts.coalesce) });
        }
        model.redo = [];
      }
      return { key, from, to: value };
    };

    model.undoStep = function undoStep() {
      const step = model.undo.pop();
      if (!step) return null;
      model.redo.push(step);
      model.setValue(step.key, step.from, { record: false });
      return { key: step.key, value: step.from };
    };

    model.redoStep = function redoStep() {
      const step = model.redo.pop();
      if (!step) return null;
      model.undo.push(step);
      model.setValue(step.key, step.to, { record: false });
      return { key: step.key, value: step.to };
    };

    /** Modifications à envoyer : valeur attendue = dernière valeur connue en base. */
    model.changes = function changes(force) {
      return Array.from(model.pending, ([key, value]) => {
        const item = model.byKey.get(key);
        const change = { file_path: item.file, xpath: item.xpath, new_value: value };
        if (!(force && force.has(key))) change.expected_value = item.value;
        return change;
      });
    };

    /**
     * Résultats du lot (statut par modification) -> état local cohérent avec la base. Ce
     * qui a été saisi pendant l'enregistrement (suite de frappe, retour en arrière) reste
     * une modification en attente : l'aperçu et le modèle ne divergent jamais.
     */
    model.applyResults = function applyResults(results, sent) {
      const outcome = { saved: [], conflicts: [], failed: [] };
      sent.forEach((change, index) => {
        const result = results[index] || { status: 'updated' };
        const key = keyOf(change.file_path, change.xpath);
        const item = model.byKey.get(key);
        if (!item) return;
        const sameAsSent = result.status === 'conflict' && String(result.current_value) === change.new_value;
        if (result.status === 'updated' || result.status === 'unchanged' || sameAsSent) {
          const intended = model.current(key);
          item.value = change.new_value;
          if (intended === change.new_value) model.pending.delete(key);
          else model.pending.set(key, intended);
          model.problems.delete(key);
          outcome.saved.push(key);
        } else if (result.status === 'conflict') {
          model.problems.set(key, { status: 'conflict', current: result.current_value == null ? '' : String(result.current_value) });
          outcome.conflicts.push(key);
        } else {
          model.problems.set(key, {
            status: result.status,
            detail: result.detail || (result.status === 'not_found' ? 'Élément introuvable en base.' : 'Valeur refusée.')
          });
          outcome.failed.push(key);
        }
      });
      return outcome;
    };

    /** Conflit : adopter la version en ligne (la modification locale est abandonnée). */
    model.adoptRemote = function adoptRemote(key) {
      const item = model.byKey.get(key);
      const problem = model.problems.get(key);
      if (!item || !problem || problem.status !== 'conflict') return;
      item.value = problem.current;
      model.pending.delete(key);
      model.problems.delete(key);
      // Les étapes d'annulation de ce texte reposaient sur la version abandonnée.
      model.undo = model.undo.filter((step) => step.key !== key);
      model.redo = model.redo.filter((step) => step.key !== key);
    };

    model.remove = function remove(key) {
      model.items = model.items.filter((item) => item.key !== key);
      model.byKey.delete(key);
      model.pending.delete(key);
      model.problems.delete(key);
    };

    return model;
  }

  window.StudioModel = {
    createModel, keyOf, norm, describeXPath, pagePath, displayPath, comparePages, labelOf, seoLimit, ORPHAN_PREFIX
  };
})(window);
