window.Aizy.onPage(signal => {
  const priceForm = document.querySelector('[data-price-inline]');
  let closePrice = () => {};
  if (priceForm) {
    const button = priceForm.querySelector('[data-price-confirm]');
    const fields = priceForm.querySelector('[data-price-fields]');
    const input = priceForm.querySelector('input[name="price"]');
    const cancel = priceForm.querySelector('[data-price-cancel]');
    button.setAttribute('aria-controls', fields.id);
    function setEditing(editing) {
      priceForm.dataset.editing = String(editing);
      fields.hidden = !editing;
      input.disabled = !editing;
      button.setAttribute('aria-expanded', String(editing));
    }
    closePrice = () => { setEditing(false); input.value = input.defaultValue; };
    cancel.hidden = false;
    cancel.addEventListener('click', () => { closePrice(); button.focus({preventScroll:true}); });
    priceForm.addEventListener('submit', event => {
      if (priceForm.dataset.editing === 'true') return;
      event.preventDefault();
      panels.forEach(panel => { panel.open = false; });
      setEditing(true);
      input.focus({preventScroll:true});
      input.select();
    });
    setEditing(priceForm.dataset.editing === 'true');
  }
  const panels = [...document.querySelectorAll('.action-panel')];
  panels.forEach(panel => panel.querySelector('summary').addEventListener('click', () => {
    closePrice();
    panels.forEach(other => { if (other !== panel) other.open = false; });
  }));
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      if (priceForm?.contains(e.target)) { closePrice(); priceForm.querySelector('[data-price-confirm]').focus({preventScroll:true}); }
      panels.forEach(panel => { panel.open = false; });
    }
    if (e.target.closest('input,textarea,select,button,summary,a') || e.altKey || e.ctrlKey || e.metaKey) return;
    const next = document.querySelector(e.key === 'ArrowRight' ? '.review-peek.next' : e.key === 'ArrowLeft' ? '.review-peek.previous' : '.no-target');
    if (next) { e.preventDefault(); next.click(); }
  }, {signal});
});
