(() => {
  let busy = false;
  const selected = scope => [...scope.querySelectorAll('[data-bulk-item]:checked')];
  function updateSelection(scope) {
    const count = selected(scope).length;
    const total = scope.querySelectorAll('[data-bulk-item]').length;
    scope.querySelector('[data-bulk-count]').textContent = `已选 ${count} 条`;
    scope.querySelector('[data-bulk-open]').disabled = count === 0;
    const all = scope.querySelector('[data-bulk-all]');
    all.checked = total > 0 && count === total;
    all.indeterminate = count > 0 && count < total;
    all.disabled = total === 0;
  }
  document.addEventListener('change', event => {
    const scope = event.target.closest('[data-bulk-scope]');
    if (!scope) return;
    if (event.target.matches('[data-bulk-all]')) {
      scope.querySelectorAll('[data-bulk-item]').forEach(item => {
        item.checked = event.target.checked;
      });
    }
    if (event.target.matches('[data-bulk-all], [data-bulk-item]')) updateSelection(scope);
    if (event.target.matches('[data-bulk-target]')) {
      const form = event.target.form;
      const option = event.target.selectedOptions[0];
      const needsMethod = event.target.value === 'download_pending';
      form.querySelector('[data-bulk-method-label]').hidden = !needsMethod;
      form.elements.download_method.disabled = !needsMethod;
      form.elements.download_method.required = needsMethod;
      const needsReplacement = event.target.value === 'outdated';
      form.querySelector('[data-bulk-replacement-label]').hidden = !needsReplacement;
      form.elements.superseded_by_id.disabled = !needsReplacement;
      form.elements.superseded_by_id.required = needsReplacement;
      const needsReason = option.dataset.requiresReason === 'yes';
      form.elements.reason.required = needsReason;
      form.querySelector('[data-bulk-reason-label]').textContent = needsReason
        ? '公共原因（必填）' : '公共原因（可选）';
      form.querySelector('[data-bulk-description]').textContent = option.dataset.description || '';
    }
  });
  document.addEventListener('click', event => {
    const opener = event.target.closest('[data-bulk-open]');
    if (!opener) return;
    const scope = opener.closest('[data-bulk-scope]');
    scope.querySelector('[data-bulk-dialog-count]').textContent = selected(scope).length;
    scope.querySelector('[data-bulk-message]').textContent = '';
    scope.querySelector('dialog').showModal();
  });
  // Keep the selected page in place until the request and result refresh finish.
  document.addEventListener('htmx:beforeRequest', event => {
    if (busy) event.preventDefault();
  });
  document.addEventListener('cancel', event => {
    if (busy && event.target.id === 'bulk-status-dialog') event.preventDefault();
  }, true);
  document.addEventListener('click', event => {
    if (busy && event.target.closest('#bulk-status-dialog')) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);

  function showResults(scope, data, refreshError) {
    const panel = scope.querySelector('[data-bulk-result]');
    const counts = {success: 0, skipped: 0, failed: 0};
    data.results.forEach(item => counts[item.outcome]++);
    const summary = document.createElement('p');
    summary.textContent = `成功 ${counts.success} 条，跳过 ${counts.skipped} 条，失败 ${counts.failed} 条。`
      + (refreshError ? ' 列表刷新失败，请手动刷新后再操作。' : ' 列表已刷新，选择已清空。');
    const details = document.createElement('details');
    details.open = counts.failed > 0;
    const heading = document.createElement('summary');
    heading.textContent = '查看逐条结果';
    const list = document.createElement('ul');
    const labels = {success: '成功', skipped: '跳过', failed: '失败'};
    data.results.forEach(item => {
      const entry = document.createElement('li');
      entry.textContent = `${item.manga_id} · ${labels[item.outcome]} · ${item.message}`;
      list.append(entry);
    });
    details.append(heading, list);
    panel.replaceChildren(summary, details);
    panel.hidden = false;
    if (refreshError) {
      scope.querySelectorAll('[data-bulk-item]').forEach(item => { item.checked = false; });
      updateSelection(scope);
    }
  }
  document.addEventListener('submit', async event => {
    const form = event.target;
    if (!form.matches('[data-bulk-form]')) return;
    event.preventDefault();
    if (busy) return;
    let scope = form.closest('[data-bulk-scope]');
    const items = selected(scope).map(item => [item.value, Number(item.dataset.version)]);
    const message = form.querySelector('[data-bulk-message]');
    if (!items.length) { message.textContent = '请先勾选档案。'; return; }
    if (!form.reportValidity()) return;
    const payload = {
      items, target_status: form.elements.target_status.value,
      reason: form.elements.reason.value,
      download_method: form.elements.download_method.disabled ? null : form.elements.download_method.value,
      superseded_by_id: form.elements.superseded_by_id.disabled ? null : form.elements.superseded_by_id.value.trim(),
    };
    busy = true;
    form.querySelectorAll('button').forEach(button => { button.disabled = true; });
    message.textContent = '正在逐条处理，请稍候…';
    try {
      const response = await fetch('/api/bulk-status', {
        method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json',
          'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content},
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) {
        message.textContent = typeof data.detail === 'string' ? data.detail : '提交参数无效，请检查后重试。';
        return;
      }
      form.closest('dialog').close();
      let refreshError = false;
      try {
        const response = await fetch(location.href, {
          headers: {'HX-Request': 'true', 'HX-Target': scope.id}, cache: 'no-store',
        });
        if (!response.ok) throw new Error('refresh');
        const documentResult = new DOMParser().parseFromString(await response.text(), 'text/html');
        const replacement = documentResult.getElementById(scope.id);
        if (!replacement) throw new Error('refresh');
        scope.replaceWith(replacement);
        scope = replacement;
        window.htmx.process(scope);
        const url = new URL(location.href);
        if (url.searchParams.has('page')) {
          url.searchParams.set('page', scope.dataset.page);
          history.replaceState(history.state, '', url);
        }
      } catch { refreshError = true; }
      showResults(scope, data, refreshError);
    } catch {
      message.textContent = '未能取得操作结果，部分档案可能已修改。请关闭弹窗并刷新核对后再操作。';
    } finally {
      busy = false;
      form.querySelectorAll('button').forEach(button => { button.disabled = false; });
    }
  });
})();
