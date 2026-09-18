(() => {
  const root = document.querySelector('[data-config-editor]');
  if (!root || root.dataset.ready) return;
  root.dataset.ready = 'true';
  const form = root.querySelector('form');
  if (!form) return;
  const status = root.querySelector('#config-status');
  let dirty = false;
  function changed() {
    dirty = true;
    form.querySelector('[data-dirty-note]').textContent = '有未保存的修改';
  }
  form.addEventListener('input', event => {
    changed();
    const reset = form.elements.namedItem('reset__' + event.target.name);
    if (reset) reset.remove();
  });
  form.querySelectorAll('[data-reset]').forEach(button => {
    button.addEventListener('click', () => {
      const input = form.elements.namedItem(button.dataset.reset);
      if (button.dataset.kind === 'bool') input.checked = button.dataset.default.toLowerCase() === 'true';
      else input.value = button.dataset.default;
      let marker = form.elements.namedItem('reset__' + input.name);
      if (!marker) {
        marker = document.createElement('input');
        marker.type = 'hidden'; marker.name = 'reset__' + input.name; form.append(marker);
      }
      marker.value = 'true';
      button.closest('[data-field]').querySelector('[data-override-label]').textContent = '保存后使用默认值';
      changed();
    });
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    event.stopPropagation();
    const button = form.querySelector('[type=submit]');
    button.disabled = true;
    root.querySelectorAll('.config-field-error').forEach(element => { element.hidden = true; });
    root.querySelectorAll('[aria-invalid]').forEach(element => element.removeAttribute('aria-invalid'));
    try {
      const response = await fetch(form.action, {
        method: 'POST', body: new FormData(form), credentials: 'same-origin',
        headers: {Accept: 'application/json'},
      });
      if (response.redirected && new URL(response.url).pathname === '/login') {
        throw new Error('登录已过期。请在新标签页登录后重试，当前输入尚未保存。');
      }
      const data = await response.json();
      if (!response.ok) {
        for (const [name, message] of Object.entries(data.fields || {})) {
          const input = form.elements.namedItem(name);
          if (!input) continue;
          input.setAttribute('aria-invalid', 'true');
          const field = input.closest('[data-field]');
          const error = field.querySelector('.config-field-error');
          error.textContent = message; error.hidden = false;
          const details = field.closest('details');
          if (details) details.open = true;
        }
        throw new Error(data.detail || '保存失败，请检查配置。');
      }
      dirty = false;
      window.location.assign(data.redirect);
    } catch (error) {
      status.hidden = false;
      status.className = 'alert error';
      status.querySelector('[data-status-message]').textContent = error.message;
      status.querySelectorAll('a').forEach(link => link.remove());
      status.focus(); status.scrollIntoView({block: 'center', behavior: 'smooth'});
      button.disabled = false;
    }
  });
  window.addEventListener('beforeunload', event => {
    if (root.isConnected && dirty) { event.preventDefault(); event.returnValue = ''; }
  });
})();
