/* Progressive navigation: Django remains the authority for data and permissions. */
(() => {
  class NavigationError extends Error {}
  const initializers = [];
  let lifecycle = new AbortController();
  let started = false;
  let active = null;
  let pendingHistory = null;
  let currentUrl = location.href;
  let noticeTimer;
  const main = () => document.querySelector('main.app-content');
  function initialize() {
    initializers.forEach(fn => fn(lifecycle.signal));
  }
  window.Aizy = {
    onPage(fn) {
      initializers.push(fn);
      if (started) fn(lifecycle.signal);
    },
    refresh() { if (!active) visit(location.href, {replace:true, preserve:true}); },
  };
  function remember() {
    history.replaceState({...history.state, aizyScroll:[scrollX, scrollY]}, '', currentUrl);
  }
  function announce(text, error = false, temporary = false) {
    clearTimeout(noticeTimer);
    const notice = document.getElementById('navigation-notice');
    notice.textContent = text;
    notice.classList.toggle('is-error', error);
    notice.hidden = !text;
    if (temporary && text) noticeTimer = setTimeout(() => announce(''), 6000);
  }
  function localUrl(value) {
    const url = new URL(value, location.href);
    return url.origin === location.origin && /^https?:$/.test(url.protocol) ? url : null;
  }
  function nativeUrl(url) {
    return /\/(connexion|deconnexion|mon-mot-de-passe|fichier|document)\//.test(url.pathname)
      || url.searchParams.has('download');
  }
  function focusContent(container, error) {
    const target = error ? container.querySelector('.app-messages [role="alert"], .errorlist, [aria-invalid="true"]') :
      container.querySelector('.review-card h2, h1, h2');
    if (target) { target.tabIndex = -1; target.focus({preventScroll:true}); }
  }
  async function synchronizeInterfaceStyles(next) {
    const selector = 'link[rel="stylesheet"][href*="/css/interface.css"]';
    const current = document.querySelector(selector), fresh = next.querySelector(selector);
    if (!current || !fresh || current.href === fresh.href) return;
    await new Promise((resolve, reject) => {
      current.addEventListener('load', resolve, {once:true});
      current.addEventListener('error', () => reject(new NavigationError('Le style de la page n’a pas pu être chargé. Actualisez la page.')), {once:true});
      current.href = fresh.href;
    });
  }
  async function visit(href, options = {}) {
    if (!main()) { location.assign(href); return; }
    if (active) return;
    const {method = 'GET', body, form, preserve = false, replace = false, fromHistory = false} = options;
    const scroll = options.scroll || (preserve ? [scrollX, scrollY] : [0, 0]);
    const openPanels = [...main().querySelectorAll('details[open]')].map(el => el.id || el.className);
    const reviewInput = form?.closest('.review-card') ? [...form.querySelectorAll('input:not([type=hidden]):not([type=file]), textarea, select')].map(el => ({name:el.name, value:el.value, checked:el.checked})) : [];
    const reviewAction = form?.querySelector('input[name=action]')?.value;
    const oldCard = main().querySelector('[data-review-line]')?.dataset.reviewLine;
    const controls = form ? [...form.querySelectorAll('button, input[type="submit"]')].map(el => [el, el.disabled]) : [];
    if (!fromHistory) remember();
    active = {method};
    main().setAttribute('aria-busy', 'true');
    document.documentElement.classList.add('is-navigating');
    controls.forEach(([el]) => { el.disabled = true; });
    announce(method === 'POST' ? 'Enregistrement…' : 'Chargement…');
    try {
      const response = await fetch(href, {method, body, credentials:'same-origin',
        headers:{'Accept':'text/html', 'X-Aizy-Navigation':'1'}});
      if (response.status === 403) throw new NavigationError('Accès refusé ou session expirée. Actualisez la page pour vous reconnecter ; aucune nouvelle tentative automatique.');
      if (response.status >= 500) throw new NavigationError(method === 'POST'
        ? 'La confirmation de l’enregistrement n’a pas pu être reçue. Vérifiez le résultat avant de réessayer.'
        : 'La page est temporairement indisponible. Vous pouvez réessayer.');
      if (!response.headers.get('Content-Type')?.includes('text/html') || response.headers.has('Content-Disposition')) {
        if (method === 'GET') { location.assign(response.url); return; }
        throw new NavigationError('Vérifiez le résultat de l’action avant de réessayer.');
      }
      const next = new DOMParser().parseFromString(await response.text(), 'text/html');
      const content = next.querySelector('main.app-content');
      if (!content) {
        if (method === 'GET' || response.redirected) { location.assign(response.url); return; }
        throw new NavigationError('La réponse n’a pas pu être affichée. Vérifiez le résultat avant de réessayer.');
      }
      // Only known initializers run; never execute scripts from a fetched document.
      // DOMParser treats noscript as active markup; its fallback styles would
      // hide the camera/file choices when entering the deposit page via AJAX.
      content.querySelectorAll('script, noscript').forEach(node => node.remove());
      await synchronizeInterfaceStyles(next);
      lifecycle.abort();
      lifecycle = new AbortController();
      main().replaceChildren(...content.childNodes);
      for (const selector of ['.profile-panel', '.app-page-heading']) {
        const old = document.querySelector(selector), fresh = next.querySelector(selector);
        if (old && fresh) { old.replaceChildren(...fresh.childNodes); if (selector === '.app-page-heading') old.hidden = fresh.hidden; }
      }
      document.title = next.title;
      const destination = new URL(response.url);
      if (!destination.hash) destination.hash = new URL(href, location.href).hash;
      if (!fromHistory) history[replace || method === 'POST' ? 'replaceState' : 'pushState']({}, '', destination.href);
      currentUrl = destination.href;
      if (fromHistory && location.href !== currentUrl) history.replaceState({}, '', currentUrl);
      const hasError = Boolean(main().querySelector('.alert-error, .alert-danger, .alert-warning, .errorlist, [aria-invalid="true"]'));
      const sameCard = oldCard === main().querySelector('[data-review-line]')?.dataset.reviewLine;
      if (preserve && sameCard && (method === 'GET' || hasError)) {
        main().querySelectorAll('details').forEach(el => { if (openPanels.includes(el.id || el.className)) el.open = true; });
      }
      if (hasError && sameCard && reviewAction) {
        const targetForm = [...main().querySelectorAll('form')].find(el => el.querySelector('input[name=action]')?.value === reviewAction);
        if (targetForm?.matches('[data-price-inline]')) targetForm.dataset.editing = 'true';
        for (const input of reviewInput) {
          const field = targetForm?.elements.namedItem(input.name);
          if (field) { field.value = input.value; if ('checked' in field) field.checked = input.checked; }
        }
      }
      initialize();
      focusContent(main(), hasError);
      scrollTo({left:scroll[0], top:scroll[1], behavior:'instant'});
      if (destination.hash) document.getElementById(decodeURIComponent(destination.hash.slice(1)))?.scrollIntoView();
      history.replaceState({...history.state, aizyScroll:[scrollX, scrollY]}, '', currentUrl);
      const feedback = method === 'POST' ? main().querySelector('.app-messages')?.textContent.trim() : '';
      announce(feedback || '', hasError, Boolean(feedback) && !hasError);
    } catch (error) {
      if (fromHistory) history.replaceState({}, '', currentUrl);
      announce(error instanceof NavigationError ? error.message
        : (method === 'POST' ? 'Connexion interrompue : vérifiez si l’action a été enregistrée avant de la relancer. Vos saisies sont conservées.' : 'Connexion interrompue. La page actuelle reste disponible ; réessayez lorsque la connexion revient.'), true);
    } finally {
      active = null;
      main()?.removeAttribute('aria-busy');
      document.documentElement.classList.remove('is-navigating');
      controls.forEach(([el, disabled]) => { el.disabled = disabled; });
      form?.dispatchEvent(new CustomEvent('aizy:submit-end'));
      if (pendingHistory) {
        const pending = pendingHistory; pendingHistory = null;
        visit(pending.href, {fromHistory:true, scroll:pending.scroll});
      }
    }
  }
  document.addEventListener('click', event => {
    const link = event.target.closest('a[href]');
    if (!main() || event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || !link
        || link.hasAttribute('download') || (link.target && link.target !== '_self') || link.closest('[data-native-navigation]')) return;
    const url = localUrl(link.href);
    if (!url || nativeUrl(url) || (url.hash && url.pathname === location.pathname && url.search === location.search)) return;
    event.preventDefault();
    if (document.querySelector('[data-deposit-busy="true"]')) return;
    visit(url.href, {preserve:url.pathname === location.pathname});
  });
  document.addEventListener('submit', event => {
    const form = event.target;
    if (!main() || event.defaultPrevented || form.closest('[data-native-navigation]') || (form.getAttribute('target') && form.getAttribute('target') !== '_self')) return;
    const submitter = event.submitter;
    const url = localUrl(submitter?.getAttribute('formaction') || form.getAttribute('action') || location.href);
    const method = (submitter?.getAttribute('formmethod') || form.getAttribute('method') || 'GET').toUpperCase();
    if (!url || nativeUrl(url) || !['GET','POST'].includes(method)) return;
    event.preventDefault();
    if (active) return;
    const data = new FormData(form);
    // Preserve the clicked button value, including buttons disabled by a local loader.
    if (submitter?.name) data.append(submitter.name, submitter.value);
    if (method === 'GET') url.search = new URLSearchParams(data).toString();
    visit(url.href, {method, body:method === 'POST' ? data : undefined, form,
      preserve:method === 'POST' || url.pathname === location.pathname});
  });
  window.addEventListener('popstate', event => {
    if (!main()) return;
    const pending = {href:location.href, scroll:event.state?.aizyScroll || [0,0]};
    if (active || document.querySelector('[data-deposit-busy="true"]')) { pendingHistory = pending; return; }
    visit(pending.href, {fromHistory:true, scroll:pending.scroll});
  });
  document.addEventListener('aizy:deposit-end', () => {
    if (!active && pendingHistory) {
      const pending = pendingHistory; pendingHistory = null;
      visit(pending.href, {fromHistory:true, scroll:pending.scroll});
    }
  });
  window.addEventListener('scroll', () => {
    if (!active && started && location.href === currentUrl) remember();
  }, {passive:true});
  document.addEventListener('DOMContentLoaded', () => {
    started = true;
    if (main()) { history.scrollRestoration = 'manual'; remember(); }
    initialize();
  });
})();
