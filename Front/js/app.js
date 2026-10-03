/*!
 * Cockpit WaaS — app.js
 * Couche applicative commune à toutes les pages : client API (cookie de session
 * HttpOnly, anti-CSRF par Origin), squelettes de chargement (dans le HTML des
 * pages), coque du cockpit et de l'espace client, rafraîchissement périodique
 * suspendu en arrière-plan.
 * Chaque page n'embarque que sa propre logique (script inline de son index.html).
 * 29th studio · © 2026
 */
(function (window, document) {
  'use strict';

  const Cockpit = window.Cockpit;
  const UI = window.UI;
  const { h } = UI;

  /* ----------------------------------------------------------------------
     Client API
     ---------------------------------------------------------------------- */
  class ApiError extends Error {
    constructor(status, message, data) {
      super(message);
      this.name = 'ApiError';
      this.status = status;
      this.data = data || null;
    }
  }

  const FRIENDLY = {
    0: 'Serveur injoignable. Vérifiez votre connexion.',
    409: 'Conflit : la ressource existe déjà ou a changé.',
    422: 'Données invalides.',
    429: 'Trop de requêtes. Réessayez dans un instant.',
    502: 'Service amont indisponible (502).',
    503: 'Service temporairement indisponible (503).',
    504: 'Délai de réponse dépassé (504).'
  };

  function detailText(data) {
    if (!data) return '';
    const detail = data.detail != null ? data.detail : data.message;
    if (Array.isArray(detail)) {
      // Erreurs de validation FastAPI : « champ : message »
      return detail.map((item) => {
        const where = Array.isArray(item.loc) ? item.loc.filter((part) => part !== 'body').join('.') : '';
        return where ? `${where} : ${item.msg}` : item.msg;
      }).join(' · ');
    }
    return detail ? String(detail) : '';
  }

  function goToLogin() {
    const target = window.location.pathname + window.location.search;
    window.location.replace(`/login?redirect=${encodeURIComponent(target)}`);
  }

  /**
   * App.api('/admin/api/sites', { method, body, query }) → JSON
   * 401 → retour à la connexion (session expirée). Toute erreur lève ApiError.
   */
  async function api(path, options) {
    const opts = options || {};
    let url = path;
    if (opts.query) {
      const params = new URLSearchParams();
      Object.entries(opts.query).forEach(([key, value]) => {
        if (value != null && value !== '') params.set(key, String(value));
      });
      const text = params.toString();
      if (text) url += (url.includes('?') ? '&' : '?') + text;
    }
    const init = {
      method: opts.method || 'GET',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      signal: opts.signal
    };
    if (opts.body instanceof FormData) {
      init.body = opts.body;
    } else if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }

    let response;
    try {
      response = await fetch(url, init);
    } catch (error) {
      if (error && error.name === 'AbortError') throw error;
      throw new ApiError(0, FRIENDLY[0]);
    }
    const data = await response.json().catch(() => null);
    if (response.status === 401 && !opts.allowUnauthorized) {
      goToLogin();
      throw new ApiError(401, 'Session expirée.');
    }
    if (!response.ok) {
      throw new ApiError(response.status, detailText(data) || FRIENDLY[response.status] || `Erreur ${response.status}`, data);
    }
    return data;
  }

  function toastError(error, title) {
    if (error && error.name === 'AbortError') return;
    Cockpit.toast({ title: title || 'Action impossible', message: error && error.message ? error.message : String(error), tone: 'danger', duration: 6000 });
  }

  function toastOk(title, message) {
    Cockpit.toast({ title, message, tone: 'good', duration: 3200 });
  }

  /* ----------------------------------------------------------------------
     État de chargement
     Chaque page porte son propre squelette dans son HTML (fragments
     components/skeleton/*.html) : il est peint dès le premier rendu, sans
     écran intermédiaire, puis remplacé en place par les données. `boot` ne
     fait donc que refléter l'état (aria-busy, annonce lecteur d'écran).
     ---------------------------------------------------------------------- */
  function status(message) {
    const node = document.getElementById('app-status');
    if (!node || message == null) return;
    node.textContent = message;
  }

  /** aria-busy sur la zone de contenu : le squelette est en place. */
  function setBusy(busy) {
    const main = document.getElementById('main');
    if (main) main.setAttribute('aria-busy', busy ? 'true' : 'false');
  }

  /** Fin du chargement : les données ont remplacé les squelettes dans la page. */
  function markReady() {
    setBusy(false);
    status('');
  }

  /** Échec du chargement initial : le squelette se fige (voir 15-app.css). */
  function markFailed() {
    document.body.classList.add('is-boot-error');
    setBusy(false);
  }

  /** Échec du chargement initial : message clair dans la page + bouton Réessayer. */
  function renderBootError(error) {
    const main = document.getElementById('main');
    if (!main) return;
    const retry = UI.btn('Réessayer', { variant: 'primary', icon: 'refresh', onClick: () => window.location.reload() });
    const box = UI.alert({
      tone: 'danger', bar: true, title: 'Chargement impossible',
      text: error && error.message ? error.message : 'Les données de cette page n\'ont pas pu être chargées.',
      actions: [retry]
    });
    main.prepend(h('div', { class: 'ck-stack', style: { 'margin-bottom': 'var(--ck-space-16)' } }, box));
  }

  const BOOT_TIMEOUT_MS = 15000;

  /**
   * App.boot(async (ctx) => { … }, { shell: 'admin' | 'portal' | null })
   * Le squelette de la page (déjà dans le HTML) reste affiché jusqu'à la fin du
   * chargement : coque + données le remplacent en place, sans écran intercalé.
   * ctx : { status(msg), shell } — shell = données de la coque (portail : vue d'ensemble).
   */
  async function boot(load, options) {
    const opts = options || {};
    const ready = document.readyState === 'loading'
      ? new Promise((resolve) => document.addEventListener('DOMContentLoaded', resolve, { once: true }))
      : Promise.resolve();
    await ready;
    markActiveNav();
    setBusy(true);

    let timer = 0;
    const timeout = new Promise((_, reject) => {
      timer = window.setTimeout(() => reject(new ApiError(504, 'Le serveur met trop de temps à répondre. Les données s\'afficheront dès réception.')), BOOT_TIMEOUT_MS);
    });

    const run = async () => {
      status(opts.message || 'Connexion au cockpit…');
      let shell = null;
      if (opts.shell === 'admin') {
        // La coque masque une session expirée en 404 (« zéro fuite ») : retour à la connexion, sans message d'erreur.
        const shellPromise = adminShell().catch((error) => {
          if (error.status === 404) {
            goToLogin();
            throw new ApiError(401, 'Session expirée.');
          }
          throw error;
        });
        const [shellData] = await Promise.all([shellPromise, Promise.resolve(load ? load({ status, shell: null }) : null)]);
        shell = shellData;
        return shell;
      }
      if (opts.shell === 'portal') {
        status('Chargement de votre espace…');
        shell = await portalShell();
        if (load) await load({ status, shell });
        return shell;
      }
      if (load) await load({ status, shell: null });
      return shell;
    };

    try {
      const result = await Promise.race([run(), timeout]);
      window.clearTimeout(timer);
      markReady();
      return result;
    } catch (error) {
      window.clearTimeout(timer);
      markFailed();
      if (error && error.status === 401) return null;
      renderBootError(error);
      toastError(error, 'Chargement incomplet');
      return null;
    }
  }

  /* ----------------------------------------------------------------------
     Navigation : lien actif selon l'URL (pas d'état dupliqué dans le HTML)
     ---------------------------------------------------------------------- */
  function markActiveNav() {
    const path = window.location.pathname.replace(/\/+$/, '') || '/';
    let best = null;
    document.querySelectorAll('[data-nav]').forEach((link) => {
      const target = link.getAttribute('data-nav');
      const match = path === target || path.startsWith(`${target}/`);
      if (match && (!best || target.length > best.getAttribute('data-nav').length)) best = link;
    });
    document.querySelectorAll('[data-nav]').forEach((link) => {
      const active = link === best;
      link.classList.toggle('is-active', active);
      if (active) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
  }

  function setText(selector, text) {
    document.querySelectorAll(selector).forEach((node) => { node.textContent = text; });
  }

  /* ----------------------------------------------------------------------
     Coque du cockpit agence (sidebar + topbar), rafraîchie toutes les 30 s
     ---------------------------------------------------------------------- */
  let lastAlerts = null;

  async function adminShell() {
    const data = await api('/admin/api/shell');
    applyAdminShell(data);
    storeShellCache('admin', data);
    if (!adminShell.polling) {
      // Le rafraîchissement entretient aussi le cache : sans cela, une page
      // laissée ouverte au-delà du TTL ferait réapparaître les défauts à la
      // navigation suivante.
      adminShell.polling = poll(() => api('/admin/api/shell')
        .then((fresh) => { applyAdminShell(fresh); storeShellCache('admin', fresh); })
        .catch(() => {}), 30000);
    }
    return data;
  }

  function applyAdminShell(data) {
    if (!data) return;
    const user = data.user || {};
    setText('[data-shell="username"]', user.username || 'Administrateur');
    setText('[data-shell="avatar"]', UI.initials(user.username || 'A'));

    const env = document.querySelector('[data-shell="env"]');
    if (env && data.environment) {
      env.hidden = false;
      env.title = `Environnement réseau : ${data.environment.label}`;
      UI.replace(env, UI.tag(data.environment.label, { tone: data.environment.mode === 'production' ? 'accent' : 'good', icon: 'globe' }));
    }
    applyLive(data.live);

    const open = Number(data.open_tickets) || 0;
    document.querySelectorAll('[data-shell="open-tickets"]').forEach((node) => {
      node.hidden = open <= 0;
      node.textContent = String(open);
    });

    const alerts = data.alerts || {};
    lastAlerts = alerts;
    const card = document.querySelector('[data-shell="alerts"]');
    if (card) {
      card.dataset.tone = alerts.tone || 'good';
      card.setAttribute('aria-label', `${alerts.title || 'Alertes'} : ${alerts.text || ''}`);
    }
    setText('[data-shell="alerts-title"]', alerts.title || 'Alertes');
    setText('[data-shell="alerts-text"]', alerts.text || 'Tout est nominal');
  }

  function applyLive(value) {
    const n = Number(value) || 0;
    document.querySelectorAll('[data-shell="live"]').forEach((node) => node.classList.toggle('is-idle', n <= 0));
    setText('[data-shell="live-text"]', `${n} en direct`);
  }

  document.addEventListener('click', (event) => {
    const card = event.target instanceof Element && event.target.closest('[data-shell="alerts"]');
    if (!card) return;
    const alerts = lastAlerts || {};
    if (!alerts.count) {
      Cockpit.toast({ title: 'Statut nominal', message: alerts.message || 'Aucun incident en cours.', tone: 'good' });
      return;
    }
    Cockpit.toast({ title: alerts.text, message: alerts.message, tone: alerts.tone === 'danger' ? 'danger' : 'warn', duration: 7000 });
    if (!window.location.pathname.startsWith('/admin/health')) {
      window.setTimeout(() => { window.location.href = '/admin/health'; }, 900);
    }
  });

  /* ----------------------------------------------------------------------
     Coque de l'espace client : site courant, sélecteur, statut, liens
     ---------------------------------------------------------------------- */
  const PORTAL_STATUS = {
    running: ['good', 'Vitrine en ligne', 'SSL & serveur opérationnels', 'Votre vitrine est en ligne et surveillée en continu.'],
    stopped: ['danger', 'Serveur arrêté', 'Vitrine hors ligne', 'Le serveur de votre vitrine est arrêté : contactez le studio pour le relancer.'],
    error: ['danger', 'Incident serveur', 'Intervention requise', 'Le serveur de votre vitrine rencontre une erreur. Le studio est alerté.'],
    restarting: ['danger', 'Incident serveur', 'Redémarrages en boucle', 'Le serveur de votre vitrine redémarre en boucle. Le studio est alerté.'],
    not_provisioned: ['warn', 'En préparation', 'Serveur en cours de création', 'Votre vitrine est en cours de configuration par le studio.']
  };

  function currentSiteId() {
    return new URLSearchParams(window.location.search).get('site') || '';
  }

  async function portalShell() {
    const siteId = currentSiteId();
    const data = await api('/api/v1/portal/overview', { query: { site_id: siteId || undefined } });
    applyPortalShell(data);
    storeShellCache('portal', data);
    return data;
  }

  function withSite(href, siteId) {
    if (!siteId) return href;
    const url = new URL(href, window.location.origin);
    url.searchParams.set('site', siteId);
    return url.pathname + url.search;
  }

  function applyPortalShell(data) {
    if (!data) return;
    const user = data.user || {};
    const site = data.site || null;
    const siteId = site ? site.site_id : '';
    setText('[data-shell="username"]', user.username || 'Client');
    setText('[data-shell="avatar"]', UI.initials(user.username || 'C'));
    setText('[data-shell="user-role"]', user.role === 'admin' ? 'Agence · vue client' : `Client · ${site ? site.name : 'Espace client'}`);
    setText('[data-shell="site-name"]', site ? site.name : 'Aucune vitrine');
    document.querySelectorAll('[data-shell="agency-nav"]').forEach((node) => { node.hidden = user.role !== 'admin'; });

    document.querySelectorAll('[data-site-link]').forEach((link) => {
      const kind = link.getAttribute('data-site-link');
      const base = kind === 'studio' ? '/studio' : (link.getAttribute('data-nav') || link.getAttribute('href') || '/portal');
      link.setAttribute('href', withSite(base, siteId));
      if (kind === 'studio' && !siteId) link.setAttribute('aria-disabled', 'true');
    });
    const visitUrl = site && site.url ? Cockpit.safeHref(site.url) : '#';
    document.querySelectorAll('[data-shell="site-visit"]').forEach((link) => {
      link.setAttribute('href', visitUrl);
      link.toggleAttribute('aria-disabled', visitUrl === '#');
      if (visitUrl === '#') link.title = 'Vitrine non publiée';
    });

    const switcher = document.querySelector('[data-shell="site-switch"]');
    if (switcher) {
      const sites = data.sites || [];
      switcher.hidden = sites.length < 2 && user.role !== 'admin';
      switcher.replaceChildren(...sites.map((s) => h('option', { value: s.site_id }, s.name)));
      switcher.value = siteId;
      if (!switcher.dataset.bound) {
        switcher.dataset.bound = '1';
        switcher.addEventListener('change', () => {
          window.location.href = withSite(window.location.pathname, switcher.value);
        });
      }
    }

    const pending = Number(data.pending_tickets) || 0;
    document.querySelectorAll('[data-shell="pending-tickets"]').forEach((node) => {
      node.hidden = pending <= 0;
      node.textContent = String(pending);
    });
    applyLive(site ? site.live_visitors : 0);

    const [tone, title, text, message] = PORTAL_STATUS[site ? site.status : 'not_provisioned'] || ['warn', 'Opération en cours', 'Opération serveur', 'Une opération est en cours sur votre serveur.'];
    const card = document.querySelector('[data-shell="site-status"]');
    if (card) {
      card.dataset.tone = tone;
      card.dataset.message = message;
      card.setAttribute('aria-label', `${title} : ${text}`);
    }
    setText('[data-shell="site-status-title"]', title);
    setText('[data-shell="site-status-text"]', text);
  }

  document.addEventListener('click', (event) => {
    const card = event.target instanceof Element && event.target.closest('[data-shell="site-status"]');
    if (!card) return;
    const tone = card.dataset.tone === 'good' ? 'good' : card.dataset.tone === 'danger' ? 'danger' : 'warn';
    Cockpit.toast({ title: card.querySelector('[data-shell="site-status-title"]').textContent, message: card.dataset.message || '', tone });
  });

  /* ----------------------------------------------------------------------
     Coque : cache de session
     La sidebar et la topbar sont identiques d'une page à l'autre, mais leurs
     valeurs (alertes, audience, compteurs, environnement) sont redemandées à
     chaque navigation : les défauts statiques étaient donc repeints puis
     remplacés — d'où un bref scintillement. On réapplique ici la dernière
     réponse connue **avant la première peinture**, puis le rafraîchissement
     habituel corrige. L'identité (nom, initiales) est rendue par le serveur et
     sert de garde-fou : le cache d'un autre compte n'est jamais réutilisé.
     ---------------------------------------------------------------------- */
  const SHELL_CACHE_PREFIX = 'waas:shell:';
  const SHELL_CACHE_SCHEMA = 1;
  const SHELL_CACHE_TTL_MS = 5 * 60 * 1000;

  function shellScope() {
    if (document.getElementById('cockpit-app')) return 'admin';
    if (document.getElementById('portal-app')) return 'portal';
    return null;
  }

  /** Sous-ensemble de la réponse réellement consommé par applyPortalShell. */
  function shellCachePayload(scope, data) {
    if (!data) return null;
    if (scope === 'admin') return data;
    const site = data.site || null;
    return {
      user: data.user || {},
      sites: data.sites || [],
      pending_tickets: Number(data.pending_tickets) || 0,
      site: site ? {
        site_id: site.site_id, name: site.name, status: site.status,
        url: site.url, live_visitors: site.live_visitors
      } : null
    };
  }

  function storeShellCache(scope, data) {
    try {
      window.sessionStorage.setItem(SHELL_CACHE_PREFIX + scope, JSON.stringify({
        s: SHELL_CACHE_SCHEMA,
        at: Date.now(),
        user: (data.user || {}).username || '',
        data: shellCachePayload(scope, data)
      }));
    } catch (error) { /* mode privé ou quota atteint : la coque marche sans cache */ }
  }

  function readShellCache(scope, expectedUser) {
    if (!expectedUser) return null;
    try {
      const raw = window.sessionStorage.getItem(SHELL_CACHE_PREFIX + scope);
      if (!raw) return null;
      const entry = JSON.parse(raw);
      if (!entry || entry.s !== SHELL_CACHE_SCHEMA || !entry.data) return null;
      if (entry.user !== expectedUser) return null;                 // autre compte : on ignore
      if (Date.now() - Number(entry.at) > SHELL_CACHE_TTL_MS) return null;
      return entry.data;
    } catch (error) {
      return null;
    }
  }

  function clearShellCache() {
    try {
      Object.keys(window.sessionStorage)
        .filter((key) => key.indexOf(SHELL_CACHE_PREFIX) === 0)
        .forEach((key) => window.sessionStorage.removeItem(key));
    } catch (error) { /* rien à purger */ }
  }

  /** Première peinture : la coque affiche la dernière valeur connue, sans aller-retour. */
  function hydrateShellFromCache() {
    const scope = shellScope();
    if (!scope) return null;
    const nameNode = document.querySelector('[data-shell="username"]');
    const data = readShellCache(scope, nameNode ? nameNode.textContent.trim() : '');
    if (!data) return null;
    if (scope === 'admin') applyAdminShell(data);
    else applyPortalShell(data);
    return data;
  }

  /* Déconnexion : aucune donnée de compte ne doit survivre à la session. */
  document.addEventListener('click', (event) => {
    const link = event.target instanceof Element && event.target.closest('a[href^="/logout"]');
    if (link) clearShellCache();
  });

  try {
    hydrateShellFromCache();
  } catch (error) {
    clearShellCache();        // cache d'une version antérieure : jamais bloquant
  }

  /* ----------------------------------------------------------------------
     Rafraîchissement périodique (suspendu quand l'onglet est masqué)
     ---------------------------------------------------------------------- */
  function poll(task, intervalMs, options) {
    const opts = options || {};
    let timer = 0;
    let running = false;
    let stopped = false;
    const tick = async () => {
      if (stopped || running || document.visibilityState === 'hidden') return;
      running = true;
      try { await task(); } finally { running = false; }
    };
    const start = () => { if (!timer && !stopped) timer = window.setInterval(tick, intervalMs); };
    const pause = () => { window.clearInterval(timer); timer = 0; };
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') { tick(); start(); } else pause();
    });
    if (opts.immediate) tick();
    start();
    return { stop() { stopped = true; pause(); }, now: tick };
  }

  /** Bouton « Actualiser » de la topbar : chaque page déclare son rechargement. */
  const refreshHandlers = [];
  function onRefresh(handler) {
    refreshHandlers.push(handler);
  }

  document.addEventListener('click', async (event) => {
    const button = event.target instanceof Element && event.target.closest('[data-app-refresh]');
    if (!button) return;
    if (!refreshHandlers.length) {
      window.location.reload();
      return;
    }
    button.classList.add('is-loading');
    button.disabled = true;
    try {
      await Promise.all(refreshHandlers.map((handler) => handler()));
      if (adminShell.polling) adminShell.polling.now();
      Cockpit.toast({ title: 'Données actualisées', tone: 'info', duration: 1600 });
    } catch (error) {
      toastError(error, 'Actualisation impossible');
    } finally {
      button.classList.remove('is-loading');
      button.disabled = false;
    }
  });

  /* ----------------------------------------------------------------------
     Divers
     ---------------------------------------------------------------------- */
  function param(name) {
    return new URLSearchParams(window.location.search).get(name);
  }

  function setParam(name, value) {
    const url = new URL(window.location.href);
    if (value == null || value === '') url.searchParams.delete(name);
    else url.searchParams.set(name, value);
    window.history.replaceState(null, '', url.pathname + url.search + url.hash);
  }

  /** Imprime un fragment (reçu) : seule la zone #print-root sort à l'impression. */
  function printNode(node) {
    let root = document.getElementById('print-root');
    if (!root) {
      root = h('div', { id: 'print-root' });
      document.body.appendChild(root);
    }
    root.replaceChildren(node.cloneNode(true));
    window.print();
  }

  window.App = {
    api, ApiError, boot, status, markReady, markFailed, toastError, toastOk, poll, onRefresh, param, setParam,
    currentSiteId, withSite, printNode, adminShell, applyPortalShell,
    cacheShell: (data) => { const scope = shellScope(); if (scope) storeShellCache(scope, data); }
  };
})(window, document);
