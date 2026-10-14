window.Aizy.onPage(signal => {
  const form = document.querySelector('[data-deposit-form]');
  if (!form) return;

  const picker = form.querySelector('#id_files');
  const camera = form.querySelector('#deposit-camera');
  const selection = form.querySelector('[data-deposit-selection]');
  const previews = form.querySelector('[data-deposit-previews]');
  const counter = form.querySelector('[data-selection-count]');
  const notice = form.querySelector('[data-deposit-notice]');
  const submit = form.querySelector('[data-deposit-submit]');
  const duplicate = form.querySelector('.deposit-duplicate');
  const commentChoice = form.querySelector('[data-delivery-comment-choice]');
  const deliveryComments = form.querySelector('[data-delivery-comments]');
  const commentFileError = form.querySelector('[data-comment-file-error]');
  const receipt = document.querySelector('[data-deposit-receipt]');
  const receiptIcon = receipt.querySelector('[data-receipt-icon]');
  const receiptTitle = receipt.querySelector('[data-receipt-title]');
  const receiptSummary = receipt.querySelector('[data-receipt-summary]');
  const standardReceipt = receipt.querySelector('[data-receipt-standard]');
  const feedbackProgress = receipt.querySelector('[data-feedback-progress]');
  const feedbackSpinner = receipt.querySelector('.delivery-feedback-spinner');
  const feedbackStatus = receipt.querySelector('[data-feedback-status]');
  const feedbackContent = receipt.querySelector('[data-feedback-content]');
  const newDeposit = receipt.querySelector('[data-deposit-new]');

  let files = [];
  let previewUrls = [];
  let busy = false;
  let feedbackBusy = false;
  let feedbackUrl = '';
  let feedbackRun = 0;
  let pollTimer;
  let canMerge = false;

  try {
    picker.files = new DataTransfer().files;
    canMerge = true;
  } catch (_) {
    /* Native file inputs remain usable. */
  }

  const selectedKind = () => form.querySelector('input[name="kind"]:checked')?.value || '';
  const wantsDeliveryFeedback = () => selectedKind() === 'bon_livraison' && Boolean(deliveryComments?.checked);
  const sameUrl = (left, right) => {
    try {
      return new URL(left, location.href).href === new URL(right, location.href).href;
    } catch (_) {
      return left === right;
    }
  };
  const redirectIfAuthenticationExpired = response => {
    let loginRedirect = false;
    try {
      loginRedirect = response.redirected && new URL(response.url).pathname.startsWith('/connexion/');
    } catch (_) {
      loginRedirect = false;
    }
    if (!loginRedirect && response.status !== 401 && response.status !== 403) return false;
    const next = `${location.pathname}${location.search}`;
    location.assign(loginRedirect ? response.url : `/connexion/?next=${encodeURIComponent(next)}`);
    return true;
  };

  function normalizeName(file) {
    if (/\.(pdf|jpe?g|png)$/i.test(file.name)) return file;
    const extension = {'image/jpeg': '.jpg', 'image/png': '.png', 'application/pdf': '.pdf'}[file.type];
    return extension
      ? new File([file], (file.name || 'document').replace(/\.[^.]+$/, '') + extension, {type:file.type,lastModified:file.lastModified})
      : file;
  }

  function syncFiles() {
    if (!canMerge) return;
    const transfer = new DataTransfer();
    files.forEach(file => transfer.items.add(file));
    picker.files = transfer.files;
    camera.value = '';
  }

  function clearPreviewUrls() {
    previewUrls.forEach(url => URL.revokeObjectURL(url));
    previewUrls = [];
  }

  function feedbackFileLimitReached() {
    return wantsDeliveryFeedback() && files.length > 1;
  }

  function render() {
    clearPreviewUrls();
    previews.replaceChildren();
    selection.hidden = files.length === 0;
    counter.textContent = `${files.length} document${files.length > 1 ? 's' : ''} prêt${files.length > 1 ? 's' : ''}`;
    if (commentFileError) {
      commentFileError.textContent = feedbackFileLimitReached()
        ? 'Le suivi des commentaires concerne un seul BL. Retirez les autres documents pour continuer.'
        : '';
    }
    submit.disabled = busy || files.length === 0 || feedbackFileLimitReached();
    if (busy) {
      submit.textContent = 'Envoi en cours…';
    } else if (wantsDeliveryFeedback()) {
      submit.textContent = 'Envoyer et commenter →';
    } else if (files.length > 1) {
      submit.textContent = `Envoyer les ${files.length} documents →`;
    } else {
      submit.textContent = 'Envoyer le document →';
    }

    files.forEach((file, index) => {
      const card = document.createElement('div');
      card.className = 'deposit-preview';
      const icon = document.createElement('div');
      icon.className = 'deposit-preview-icon';
      icon.textContent = /\.pdf$/i.test(file.name) ? 'PDF' : 'PHOTO';
      card.append(icon);
      if (/\.(jpe?g|png)$/i.test(file.name)) {
        const img = document.createElement('img');
        img.className = 'deposit-preview-image';
        img.alt = `Aperçu de ${file.name}`;
        const url = URL.createObjectURL(file);
        previewUrls.push(url);
        img.src = url;
        img.onload = () => { icon.hidden = true; };
        img.onerror = () => { img.hidden = true; icon.hidden = false; };
        card.prepend(img);
        icon.hidden = true;
      }
      const copy = document.createElement('div');
      copy.className = 'deposit-preview-copy';
      const name = document.createElement('span');
      name.className = 'deposit-preview-name';
      name.textContent = file.name;
      const size = document.createElement('span');
      size.className = 'deposit-preview-size';
      size.textContent = file.size >= 1024 * 1024
        ? `${(file.size / (1024 * 1024)).toFixed(1)} Mo`
        : `${Math.max(1, Math.round(file.size / 1024))} Ko`;
      copy.append(name, size);
      card.append(copy);
      if (canMerge) {
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'deposit-preview-remove';
        remove.textContent = '×';
        remove.setAttribute('aria-label', `Retirer ${file.name}`);
        remove.disabled = busy;
        remove.addEventListener('click', () => {
          files.splice(index, 1);
          syncFiles();
          render();
          const next = previews.querySelector('button') || form.querySelector('.camera-choice');
          next.focus();
        });
        card.append(remove);
      }
      previews.append(card);
    });
  }

  function selected(source) {
    notice.textContent = '';
    form.querySelector('[data-error="files"]').textContent = '';
    if (!canMerge) {
      files = [...picker.files, ...camera.files];
      render();
      return;
    }
    const additions = Array.from(source.files).map(normalizeName);
    const combined = files.slice();
    for (const file of additions) {
      if (!/\.(pdf|jpe?g|png)$/i.test(file.name)) {
        notice.textContent = 'Choisissez un PDF ou une photo au format JPG ou PNG.';
        syncFiles();
        return;
      }
      if (file.size === 0 || file.size > 20 * 1024 * 1024) {
        notice.textContent = `${file.name} : choisissez un fichier non vide de 20 Mo maximum.`;
        syncFiles();
        return;
      }
      if (!combined.some(old => old.name === file.name && old.size === file.size && old.lastModified === file.lastModified)) {
        combined.push(file);
      }
    }
    if (combined.length > 10 || combined.reduce((total, file) => total + file.size, 0) > 100 * 1024 * 1024) {
      notice.textContent = 'Vous pouvez envoyer 10 documents et 100 Mo maximum à la fois.';
      syncFiles();
      return;
    }
    files = combined;
    syncFiles();
    render();
  }

  function updateDeliveryCommentChoice() {
    const isDelivery = selectedKind() === 'bon_livraison';
    commentChoice.hidden = !isDelivery;
    if (!isDelivery && deliveryComments) deliveryComments.checked = false;
    render();
  }

  function stopPolling() {
    clearTimeout(pollTimer);
    pollTimer = undefined;
    feedbackRun += 1;
  }

  function schedulePoll(run, delay) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(() => pollFeedback(run), delay);
  }

  function markPendingButton(url) {
    document.querySelectorAll('[data-resume-feedback]').forEach(button => {
      const active = sameUrl(button.dataset.url, url);
      button.classList.toggle('is-active', active);
      if (active) button.setAttribute('aria-current', 'step');
      else button.removeAttribute('aria-current');
    });
  }

  function removePendingButton(url) {
    const list = document.querySelector('[data-pending-feedbacks]');
    if (!list) return;
    document.querySelectorAll('[data-resume-feedback]').forEach(button => {
      if (sameUrl(button.dataset.url, url)) button.remove();
    });
    const remaining = list.querySelectorAll('[data-resume-feedback]').length;
    const count = list.querySelector('.pending-feedbacks-heading > span');
    if (count) count.textContent = String(remaining);
    if (!remaining) list.hidden = true;
  }

  function showReceiptShell() {
    form.hidden = true;
    receipt.hidden = false;
    receipt.focus({preventScroll:true});
  }

  function showStandardReceipt(result) {
    stopPolling();
    feedbackUrl = '';
    showReceiptShell();
    receipt.dataset.feedbackState = 'received';
    receiptIcon.textContent = '✓';
    receiptTitle.textContent = result.received > 1 ? 'Documents reçus' : 'Document reçu';
    receiptSummary.textContent = `${result.received} document${result.received > 1 ? 's' : ''} ajouté${result.received > 1 ? 's' : ''} chez ${result.supplier}.`;
    standardReceipt.hidden = false;
    feedbackProgress.hidden = true;
    feedbackContent.hidden = true;
    feedbackContent.replaceChildren();
    newDeposit.hidden = false;
  }

  function showTerminalFeedback(state, message) {
    clearTimeout(pollTimer);
    receipt.dataset.feedbackState = state;
    feedbackProgress.hidden = false;
    feedbackSpinner.hidden = true;
    feedbackContent.hidden = true;
    feedbackContent.replaceChildren();
    standardReceipt.hidden = true;
    newDeposit.hidden = false;
    if (state === 'completed') {
      receiptIcon.textContent = '✓';
      receiptTitle.textContent = 'Commentaires enregistrés';
      feedbackStatus.textContent = message || 'Les problèmes de livraison ont été enregistrés.';
      removePendingButton(feedbackUrl);
    } else if (state === 'empty') {
      receiptIcon.textContent = '…';
      receiptTitle.textContent = 'Aucun produit reconnu';
      feedbackStatus.textContent = message || 'Le BL est conservé. Aucun incident n’est créé tant que ses produits ne sont pas disponibles.';
    } else {
      receiptIcon.textContent = '!';
      receiptTitle.textContent = 'Analyse à reprendre';
      feedbackStatus.textContent = message || 'Le BL est conservé. Vous pourrez reprendre ce commentaire lorsque l’analyse sera disponible.';
    }
    receipt.focus({preventScroll:true});
  }

  function showCompletedFallback(message) {
    receipt.dataset.feedbackState = 'completed';
    receiptIcon.textContent = '✓';
    receiptTitle.textContent = 'Commentaires enregistrés';
    standardReceipt.hidden = true;
    feedbackProgress.hidden = false;
    feedbackSpinner.hidden = true;
    feedbackStatus.textContent = message || 'Les problèmes de livraison ont été enregistrés.';
    feedbackContent.hidden = true;
    newDeposit.hidden = false;
  }

  function updateFeedbackSaveState(dynamicForm) {
    const checked = [...dynamicForm.querySelectorAll('[data-feedback-line]:checked')];
    const complete = checked.length > 0 && checked.every(input => {
      const textarea = input.closest('[data-feedback-product]')?.querySelector('textarea');
      return (textarea?.value.trim().length || 0) >= 5;
    });
    const save = dynamicForm.querySelector('[data-feedback-save]');
    if (save) save.disabled = feedbackBusy || !complete;
  }

  function wireFeedbackForm(dynamicForm) {
    const error = dynamicForm.querySelector('[data-feedback-form-error]');
    dynamicForm.querySelectorAll('[data-feedback-line]').forEach(input => {
      const product = input.closest('[data-feedback-product]');
      const comment = product.querySelector('[data-feedback-comment]');
      const textarea = comment.querySelector('textarea');
      const update = focus => {
        product.classList.toggle('is-selected', input.checked);
        comment.hidden = !input.checked;
        textarea.disabled = !input.checked;
        if (input.checked && focus) textarea.focus();
        if (error) error.textContent = '';
        updateFeedbackSaveState(dynamicForm);
      };
      input.addEventListener('change', () => update(true));
      textarea.addEventListener('input', () => {
        if (error) error.textContent = '';
        updateFeedbackSaveState(dynamicForm);
      });
      update(false);
    });

    dynamicForm.addEventListener('submit', async event => {
      event.preventDefault();
      if (feedbackBusy) return;
      const action = event.submitter?.value || 'save';
      if (action === 'save') {
        const checked = [...dynamicForm.querySelectorAll('[data-feedback-line]:checked')];
        if (!checked.length) {
          error.textContent = 'Sélectionnez au moins un produit.';
          return;
        }
        const missing = checked.find(input => (input.closest('[data-feedback-product]')?.querySelector('textarea')?.value.trim().length || 0) < 5);
        if (missing) {
          error.textContent = 'Décrivez chaque défaut en au moins 5 caractères.';
          missing.closest('[data-feedback-product]').querySelector('textarea').focus();
          return;
        }
      }

      const payload = new FormData(dynamicForm);
      payload.set('action', action);
      const controls = [...dynamicForm.querySelectorAll('button, input, textarea')].map(control => [control, control.disabled]);
      feedbackBusy = true;
      receipt.dataset.depositBusy = 'true';
      controls.forEach(([control]) => { control.disabled = true; });
      if (error) error.textContent = '';
      try {
        let response;
        const headers = {'Accept': 'application/json'};
        const csrfToken = dynamicForm.querySelector('[name="csrfmiddlewaretoken"]')?.value;
        if (csrfToken) headers['X-CSRFToken'] = csrfToken;
        try {
          // The submit buttons are named "action". In the DOM, that name can
          // shadow HTMLFormElement.action and turn it into a RadioNodeList.
          const submitUrl = dynamicForm.getAttribute('action') || feedbackUrl;
          response = await fetch(submitUrl, {
            method: 'POST',
            body: payload,
            credentials: 'same-origin',
            headers,
            signal,
          });
        } catch (networkError) {
          if (networkError.name !== 'AbortError' && error) {
            console.error('Enregistrement des commentaires BL impossible : serveur injoignable.', networkError);
            error.textContent = 'Le serveur est injoignable. Vos commentaires restent saisis ; vérifiez que l’application est ouverte puis réessayez.';
          }
          return;
        }
        if (redirectIfAuthenticationExpired(response)) return;
        if (!response.headers.get('content-type')?.includes('application/json')) {
          console.error('Réponse non JSON pendant l’enregistrement des commentaires BL.', {status:response.status});
          if (error) error.textContent = `Le serveur a renvoyé une réponse inattendue (HTTP ${response.status}). Vos commentaires restent saisis ; réessayez.`;
          return;
        }
        let result;
        try {
          result = await response.json();
        } catch (parseError) {
          console.error('Réponse JSON illisible pendant l’enregistrement des commentaires BL.', parseError);
          if (error) error.textContent = `La réponse du serveur n’a pas pu être lue (HTTP ${response.status}). Vos commentaires restent saisis ; réessayez.`;
          return;
        }
        if (!response.ok || result.state !== 'completed') {
          if (error) error.textContent = result.error || 'L’enregistrement n’a pas abouti. Vos commentaires restent saisis.';
          return;
        }
        try {
          showTerminalFeedback('completed', result.message);
        } catch (renderError) {
          console.error('Commentaires BL enregistrés, mais confirmation visuelle incomplète.', renderError);
          showCompletedFallback(result.message);
        }
      } finally {
        feedbackBusy = false;
        delete receipt.dataset.depositBusy;
        controls.forEach(([control, disabled]) => { control.disabled = disabled; });
        if (dynamicForm.isConnected) updateFeedbackSaveState(dynamicForm);
        document.dispatchEvent(new Event('aizy:deposit-end'));
      }
    });
  }

  async function pollFeedback(run) {
    if (signal.aborted || run !== feedbackRun || !feedbackUrl) return;
    if (document.hidden) {
      schedulePoll(run, 5000);
      return;
    }
    try {
      const response = await fetch(feedbackUrl, {
        credentials: 'same-origin',
        headers: {'Accept': 'application/json'},
        signal,
      });
      if (redirectIfAuthenticationExpired(response)) return;
      if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('invalid-response');
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'request-failed');
      if (run !== feedbackRun) return;

      if (result.state === 'processing') {
        feedbackProgress.hidden = false;
        feedbackSpinner.hidden = false;
        feedbackStatus.textContent = result.message || 'Analyse du BL en cours…';
        schedulePoll(run, 4000);
        return;
      }
      if (result.state === 'ready' && typeof result.html === 'string') {
        feedbackProgress.hidden = true;
        feedbackContent.innerHTML = result.html;
        feedbackContent.hidden = false;
        receipt.dataset.feedbackState = 'ready';
        receiptIcon.textContent = '✓';
        receiptTitle.textContent = 'Produits du BL';
        const dynamicForm = feedbackContent.querySelector('[data-delivery-feedback-form]');
        if (!dynamicForm) throw new Error('invalid-fragment');
        wireFeedbackForm(dynamicForm);
        dynamicForm.querySelector('[data-feedback-line]')?.focus();
        return;
      }
      if (result.state === 'completed') {
        showTerminalFeedback('completed', result.message);
        return;
      }
      if (result.state === 'empty') {
        showTerminalFeedback('empty', result.message);
        return;
      }
      if (result.state === 'failed') {
        showTerminalFeedback('failed', result.message);
        return;
      }
      throw new Error('unknown-state');
    } catch (error) {
      if (error.name === 'AbortError' || signal.aborted || run !== feedbackRun) return;
      feedbackProgress.hidden = false;
      feedbackSpinner.hidden = false;
      feedbackStatus.textContent = 'Connexion interrompue. Nouvelle tentative dans quelques secondes…';
      schedulePoll(run, 8000);
    }
  }

  function startFeedback(url, summary = '') {
    stopPolling();
    feedbackUrl = url || '';
    const run = feedbackRun;
    showReceiptShell();
    receipt.dataset.feedbackState = 'processing';
    receiptIcon.textContent = '…';
    receiptTitle.textContent = 'Analyse du BL en cours';
    receiptSummary.textContent = summary || 'Le fichier est enregistré. Les produits vont s’afficher ici.';
    standardReceipt.hidden = true;
    feedbackContent.hidden = true;
    feedbackContent.replaceChildren();
    feedbackProgress.hidden = false;
    feedbackSpinner.hidden = false;
    feedbackStatus.textContent = 'Analyse du BL en cours…';
    newDeposit.hidden = true;
    markPendingButton(feedbackUrl);
    if (!feedbackUrl) {
      showTerminalFeedback('failed', 'Le BL est bien enregistré, mais son suivi ne peut pas être ouvert pour le moment.');
      return;
    }
    pollFeedback(run);
  }

  picker.addEventListener('change', () => selected(picker));
  camera.addEventListener('change', () => selected(camera));
  form.querySelectorAll('input[name="kind"]').forEach(input => input.addEventListener('change', updateDeliveryCommentChoice));
  deliveryComments?.addEventListener('change', render);
  form.querySelectorAll('.deposit-choice').forEach(label => {
    label.addEventListener('keydown', event => {
      if (busy) return;
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        document.getElementById(label.htmlFor).click();
      }
    });
  });

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || !files.length || feedbackFileLimitReached() || !form.reportValidity()) return;
    form.querySelectorAll('[data-error]').forEach(node => { node.textContent = ''; });
    notice.textContent = '';
    const requestedFeedback = wantsDeliveryFeedback();
    busy = true;
    form.dataset.depositBusy = 'true';
    render();
    const payload = new FormData(form);
    const controls = [...form.querySelectorAll('input, select, textarea, button')].map(control => [control, control.disabled]);
    controls.forEach(([control]) => { control.disabled = true; });
    const choices = form.querySelectorAll('.deposit-choice');
    choices.forEach(label => { label.style.pointerEvents = 'none'; });
    try {
      const response = await fetch(form.action || location.href, {
        method: 'POST',
        body: payload,
        credentials: 'same-origin',
        headers: {'Accept': 'application/json'},
        signal,
      });
      if (redirectIfAuthenticationExpired(response)) return;
      if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('invalid-response');
      const result = await response.json();
      if (!response.ok) {
        for (const [field, errors] of Object.entries(result.errors || {})) {
          const target = [...form.querySelectorAll('[data-error]')].find(node => node.dataset.error === field) || notice;
          target.textContent = errors.map(error => error.message).join(' ');
          if (field === 'duplicate_reason') {
            duplicate.hidden = false;
            duplicate.open = true;
          }
        }
        if (!Object.keys(result.errors || {}).length) {
          notice.textContent = result.error || 'L’envoi n’a pas abouti. Vous pouvez réessayer ; vos documents restent prêts.';
        }
        return;
      }

      files = [];
      picker.value = '';
      camera.value = '';
      render();
      if (requestedFeedback) {
        const documentInfo = (result.documents || []).find(item => item.kind === 'bon_livraison' && item.feedback_url)
          || (result.documents || [])[0];
        startFeedback(documentInfo?.feedback_url, `BL ajouté chez ${result.supplier}.`);
      } else {
        showStandardReceipt(result);
      }
    } catch (error) {
      if (error.name !== 'AbortError') {
        notice.textContent = 'L’envoi n’a pas abouti. Vérifiez votre connexion puis réessayez ; vos documents restent prêts.';
      }
    } finally {
      controls.forEach(([control, disabled]) => { control.disabled = disabled; });
      busy = false;
      delete form.dataset.depositBusy;
      render();
      choices.forEach(label => { label.style.pointerEvents = ''; });
      if (duplicate.open && !form.hidden) form.querySelector('#id_duplicate_reason').focus();
      document.dispatchEvent(new Event('aizy:deposit-end'));
    }
  });

  newDeposit.addEventListener('click', () => {
    stopPolling();
    feedbackUrl = '';
    document.querySelectorAll('[data-resume-feedback].is-active').forEach(button => {
      button.classList.remove('is-active');
      button.removeAttribute('aria-current');
    });
    receipt.hidden = true;
    form.hidden = false;
    notice.textContent = '';
    feedbackContent.replaceChildren();
    feedbackContent.hidden = true;
    duplicate.open = false;
    duplicate.hidden = true;
    form.querySelector('#id_duplicate_reason').value = '';
    if (deliveryComments) deliveryComments.checked = false;
    files = [];
    picker.value = '';
    camera.value = '';
    syncFiles();
    render();
    form.querySelector('#id_fournisseur')?.focus();
  });

  document.querySelectorAll('[data-resume-feedback]').forEach(button => {
    button.addEventListener('click', () => {
      if (busy || feedbackBusy) return;
      startFeedback(button.dataset.url, button.querySelector('small')?.textContent.trim() || '');
      const behavior = matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
      receipt.scrollIntoView({block:'start', behavior});
    }, {signal});
  });

  signal.addEventListener('abort', () => {
    clearTimeout(pollTimer);
    clearPreviewUrls();
  });
  window.addEventListener('pagehide', clearPreviewUrls, {signal});
  window.addEventListener('pageshow', event => { if (event.persisted) render(); }, {signal});

  updateDeliveryCommentChoice();
  const automaticResume = document.querySelector('[data-resume-feedback][data-auto="true"]');
  if (automaticResume) {
    startFeedback(automaticResume.dataset.url, automaticResume.querySelector('small')?.textContent.trim() || '');
  } else {
    const automaticUrl = document.querySelector('[data-auto-feedback-url]')?.dataset.autoFeedbackUrl;
    if (automaticUrl) startFeedback(automaticUrl);
  }
});
