(() => {
  const form = document.querySelector('[data-metadata-selection]');
  if (!form || form.dataset.ready) return;
  form.dataset.ready = 'true';
  const input = form.elements.namedItem('manga_ids');
  const button = form.querySelector('[data-add-mismatches]');
  const status = form.querySelector('[data-selection-status]');
  const submit = form.querySelector('button[type="submit"]');
  const picker = form.querySelector('[data-metadata-picker]');
  const chips = form.querySelector('[data-selection-chips]');
  const draft = form.querySelector('[data-selection-entry]');
  const count = form.querySelector('[data-selection-count]');
  const empty = form.querySelector('[data-selection-empty]');
  const clear = form.querySelector('[data-clear-selection]');
  let selected = new Set(input.value.split(/[\s,，]+/).filter(Boolean));
  let batchLimit = null;

  function render() {
    input.value = [...selected].join('\n');
    chips.replaceChildren();
    for (const id of selected) {
      const chip = document.createElement('li');
      chip.className = 'metadata-chip';
      const label = document.createElement('span');
      label.className = 'metadata-chip-id';
      label.textContent = id;
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = '×';
      remove.dataset.removeId = id;
      remove.setAttribute('aria-label', `移除 ${id}`);
      chip.append(label, remove);
      chips.append(chip);
    }
    count.textContent = `已选择 ${selected.size} 个档案`;
    empty.hidden = selected.size > 0;
    clear.disabled = selected.size === 0;
    status.textContent = batchLimit && selected.size > batchLimit
      ? `单次最多 ${batchLimit} 个，请移除部分档案后分批处理。` : '';
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }

  function commitDraft() {
    const ids = draft.value.split(/[\s,，]+/).filter(Boolean);
    if (!ids.length) return;
    ids.forEach(id => selected.add(id));
    draft.value = '';
    render();
  }

  chips.addEventListener('click', event => {
    const remove = event.target.closest('[data-remove-id]');
    if (!remove) return;
    const index = [...selected].indexOf(remove.dataset.removeId);
    selected.delete(remove.dataset.removeId);
    render();
    const buttons = chips.querySelectorAll('button');
    (buttons[Math.min(index, buttons.length - 1)] || draft).focus();
  });
  clear.addEventListener('click', () => {
    selected.clear();
    draft.value = '';
    render();
    draft.focus();
  });
  form.querySelector('[data-add-entry]').addEventListener('click', () => {
    commitDraft();
    draft.focus();
  });
  draft.addEventListener('keydown', event => {
    if (!event.isComposing && ['Enter', ',', '，'].includes(event.key)) {
      event.preventDefault();
      commitDraft();
    }
  });
  draft.addEventListener('paste', event => {
    const text = event.clipboardData?.getData('text');
    if (!text) return;
    event.preventDefault();
    draft.setRangeText(text, draft.selectionStart, draft.selectionEnd, 'end');
    commitDraft();
  });
  draft.addEventListener('blur', commitDraft);
  // Capture before HTMX serializes the form, including an uncommitted last ID.
  form.addEventListener('submit', commitDraft, true);
  render();
  form.querySelector('[data-metadata-raw]').hidden = true;
  picker.hidden = false;

  button.addEventListener('click', async () => {
    button.disabled = submit.disabled = true;
    status.textContent = '正在读取待复核档案…';
    try {
      const response = await fetch(button.dataset.url, { headers: { Accept: 'application/json' } });
      if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) {
        throw new Error('读取失败，请刷新页面后重试。');
      }
      const data = await response.json();
      if (!Array.isArray(data.manga_ids) || data.manga_ids.some(id => typeof id !== 'string')) {
        throw new Error('返回的档案列表无效，请重试。');
      }
      commitDraft();
      const before = selected.size;
      data.manga_ids.forEach(id => selected.add(id));
      batchLimit = data.batch_limit;
      render();
      status.textContent = `已加入 ${selected.size - before} 个待复核档案。${status.textContent}`;
    } catch (error) {
      status.textContent = error.message || '读取失败，请重试。';
    } finally {
      button.disabled = submit.disabled = false;
    }
  });
})();
