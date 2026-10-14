window.Aizy.onPage(signal => {
document.querySelectorAll('[data-job-pending="1"]').forEach(panel => {
  const label = panel.querySelector('[data-job-label]');
  const message = panel.querySelector('[data-job-message]');
  async function poll() {
    if (signal.aborted || !panel.isConnected) return;
    if (document.hidden) { setTimeout(poll, 8000); return; }
    try {
      const response = await fetch(panel.dataset.jobUrl, {headers:{'Accept':'application/json'}});
      if (!response.ok) return;
      const state = await response.json();
      if (label) label.textContent = state.label;
      if (state.job === 'queued' || state.job === 'running') { setTimeout(poll, 5000); return; }
      if (message) {
        message.textContent = 'Traitement terminé. ';
        const link = document.createElement('a');
        link.href = location.href;
        link.textContent = 'Actualiser pour vérifier les données';
        message.append(link);
      }
    } catch (_) { setTimeout(poll, 10000); }
  }
  setTimeout(poll, 3000);
});
// Reduce supplier choices to the selected restaurant without losing a stored choice.
const restaurant = document.querySelector('#id_restaurant');
const supplier = document.querySelector('#id_fournisseur');
if (restaurant && supplier) {
  const filterSuppliers = () => {
    for (const option of supplier.options) {
      if (option.dataset.restaurant) {
        option.hidden = Boolean(restaurant.value) && option.dataset.restaurant !== restaurant.value;
        option.disabled = option.hidden;
        if (option.hidden && option.selected) supplier.value = '';
      }
    }
  };
  restaurant.addEventListener('change', filterSuppliers);
  filterSuppliers();
}

document.querySelectorAll('[data-correction-tabs]').forEach(tabs => {
  const layout = tabs.nextElementSibling;
  tabs.querySelectorAll('button').forEach(button => {
    button.addEventListener('click', () => {
      const pane = button.dataset.pane;
      layout.dataset.activePane = pane;
      tabs.querySelectorAll('button').forEach(item => item.setAttribute('aria-selected', String(item === button)));
    });
  });
});

// Le bouton d’ajout ouvre la saisie sans quitter le catalogue.
document.querySelectorAll('a[href="#add-product"]').forEach(link => link.addEventListener('click', () => {
  const panel = document.getElementById('add-product');
  if (panel) { panel.open = true; panel.querySelector('input:not([type="hidden"])')?.focus(); }
}));


const currency = document.querySelector('#id_devise_defaut');
if (restaurant && currency) restaurant.addEventListener('change', () => {
  const value = restaurant.selectedOptions[0]?.dataset.devise;
  if (value) currency.value = value;
});

// Keep periodic updates from discarding data while a form is being edited.
let edited = false;
document.querySelector('main')?.addEventListener('input', () => { edited = true; }, {signal});
if (document.querySelector('[data-refresh-pending]')) {
  const timer = setTimeout(() => {
    if (!signal.aborted && !edited && !document.hidden) window.Aizy.refresh();
  }, 5000);
  signal.addEventListener('abort', () => clearTimeout(timer));
}

document.querySelectorAll('[data-ai-submit-form]').forEach(form => {
  form.addEventListener('submit', event => {
    if (event.defaultPrevented) return;
    const message = form.dataset.aiConfirmMessage;
    if (message && !window.confirm(message)) { event.preventDefault(); return; }
    const nodes = [...form.querySelectorAll('[data-ai-submit-label], [data-ai-submit-status], [data-ai-submit-spinner]')];
    const original = nodes.map(node => [node, node.textContent, node.hidden]);
    const label = form.querySelector('[data-ai-submit-label]');
    const status = form.querySelector('[data-ai-submit-status]');
    const spinner = form.querySelector('[data-ai-submit-spinner]');
    if (label) label.textContent = form.dataset.aiLoadingLabel || 'Envoi…';
    if (status) { status.textContent = form.dataset.aiLoadingStatus || ''; status.hidden = false; }
    if (spinner) spinner.hidden = false;
    form.addEventListener('aizy:submit-end', () => {
      original.forEach(([node, text, hidden]) => { node.textContent = text; node.hidden = hidden; });
    }, {once:true});
  });
});
});
