// Delegated handlers survive updates of the main content and profile controls.
document.addEventListener('click', event => {
  const button = event.target.closest('[data-password-toggle]');
  if (!button) return;
  const input = button.parentElement.querySelector('input');
  const show = input.type === 'password';
  input.type = show ? 'text' : 'password';
  button.textContent = show ? 'Masquer' : 'Afficher';
  button.setAttribute('aria-label', show ? 'Masquer le mot de passe' : 'Afficher le mot de passe');
  button.setAttribute('aria-pressed', String(show));
});
