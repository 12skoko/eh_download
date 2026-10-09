/* EH Archive 控制台 v3 —— 页面模块：档案 ID 选择器、视频风险确认、配置编辑、日志查看、系统管理。 */
(() => {
  "use strict";
  if (window.uiv3Pages) return;
  window.uiv3Pages = true;
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content || "";
  const toast = (message, tone) => window.uiv3?.toast(message, tone);
  const loginUrl = () => `/uiv3/login?next=${encodeURIComponent(location.pathname + location.search)}`;
  const once = (element, key) => {
    if (!element || element.dataset[key]) return false;
    element.dataset[key] = "1";
    return true;
  };

  /* ================= 档案 ID 选择器（LANraragi 元数据更新） ================= */
  function initPicker(form) {
    if (!once(form, "pickerReady")) return;
    const raw = form.elements.namedItem("manga_ids");
    const picker = $("[data-picker]", form);
    const chips = $("[data-picker-chips]", form);
    const entry = $("[data-picker-entry]", form);
    const count = $("[data-picker-count]", form);
    const clear = $("[data-picker-clear]", form);
    const status = $("[data-picker-status]", form);
    const mismatch = $("[data-picker-mismatch]", form);
    const submit = $("button[type=submit]", form);
    const split = (value) => value.split(/[\s,，]+/).filter(Boolean);
    const selected = new Set(split(raw.value));
    let limit = null;
    function render() {
      raw.value = [...selected].join("\n");
      chips.replaceChildren(...[...selected].map((id) => {
        const chip = document.createElement("li");
        chip.className = "id-chip";
        const label = document.createElement("span");
        label.className = "mono";
        label.textContent = id;
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "×";
        remove.dataset.removeId = id;
        remove.setAttribute("aria-label", `移除 ${id}`);
        chip.append(label, remove);
        return chip;
      }));
      count.textContent = `已选择 ${selected.size} 个档案`;
      clear.disabled = selected.size === 0;
      if (limit && selected.size > limit) status.textContent = `单次最多 ${limit} 个，请移除部分档案后分批处理。`;
    }
    function commit() {
      const ids = split(entry.value);
      if (!ids.length) return;
      ids.forEach((id) => selected.add(id));
      entry.value = "";
      render();
    }
    chips.addEventListener("click", (event) => {
      const button = event.target.closest("[data-remove-id]");
      if (!button) return;
      selected.delete(button.dataset.removeId);
      render();
      entry.focus();
    });
    clear.addEventListener("click", () => { selected.clear(); entry.value = ""; status.textContent = ""; render(); entry.focus(); });
    entry.addEventListener("keydown", (event) => {
      if (!event.isComposing && ["Enter", ",", "，"].includes(event.key)) { event.preventDefault(); commit(); }
      if (event.key === "Backspace" && !entry.value && selected.size) {
        selected.delete([...selected].pop());
        render();
      }
    });
    entry.addEventListener("paste", (event) => {
      const text = event.clipboardData?.getData("text");
      if (!text) return;
      event.preventDefault();
      entry.setRangeText(text, entry.selectionStart, entry.selectionEnd, "end");
      commit();
    });
    entry.addEventListener("blur", commit);
    $(".chip-box", form)?.addEventListener("click", (event) => { if (event.target.matches(".chip-box, .id-chips")) entry.focus(); });
    document.addEventListener("submit", (event) => { if (event.target === form) commit(); }, true);
    mismatch.addEventListener("click", async () => {
      mismatch.disabled = submit.disabled = true;
      status.textContent = "正在读取待复核档案…";
      try {
        const response = await fetch(form.dataset.mismatchUrl, {headers: {Accept: "application/json"}, credentials: "same-origin"});
        if (!response.ok || !response.headers.get("content-type")?.includes("application/json")) throw new Error("读取失败，请刷新页面后重试。");
        const data = await response.json();
        if (!Array.isArray(data.manga_ids) || data.manga_ids.some((id) => typeof id !== "string")) throw new Error("返回的档案列表无效，请重试。");
        commit();
        const before = selected.size;
        data.manga_ids.forEach((id) => selected.add(id));
        limit = data.batch_limit;
        status.textContent = `已加入 ${selected.size - before} 个待复核档案。`;
        render();
      } catch (error) {
        status.textContent = error.message || "读取失败，请重试。";
      } finally {
        mismatch.disabled = submit.disabled = false;
      }
    });
    $("[data-picker-raw]", form).hidden = true;
    picker.hidden = false;
    render();
  }

  /* ================= 视频种子：只显示相关风险 ================= */
  function initRisk(form) {
    if (!once(form, "riskReady")) return;
    const text = $("[data-risk-text]", form);
    function update() {
      const required = new Set();
      for (const role of ["image", "video"]) {
        const choice = form.querySelector(`input[name="${role}_choice_id"]:checked`);
        (choice?.dataset.warnings || "").split(",").filter(Boolean).forEach((warning) => required.add(`${role}:${warning}`));
      }
      $$("[data-risk]", form).forEach((box) => {
        const needed = required.has(box.dataset.risk);
        box.hidden = !needed;
        const input = $("input", box);
        input.disabled = !needed;
        input.required = needed;
      });
      const picked = form.querySelector('input[name="image_choice_id"]:checked') || form.querySelector('input[name="video_choice_id"]:checked');
      text.textContent = !picked ? "选择候选后会在这里列出需要确认的风险。"
        : required.size ? `所选候选带有 ${required.size} 项风险，需要逐项确认后才能提交。` : "所选候选没有风险标记，可以直接提交。";
      form.querySelector("[data-risk-hint]").dataset.tone = required.size ? "warn" : "info";
    }
    form.addEventListener("change", update);
    update();
  }

  /* ================= 配置编辑 ================= */
  function initConfig(root) {
    const form = $("[data-config-form]", root);
    if (!form || !once(form, "configReady")) return;
    const status = $("#config-status", root);
    const note = $("[data-dirty-note]", form);
    let dirty = false;
    const changed = () => {
      dirty = true;
      if (note) { note.textContent = "有未保存的修改"; note.dataset.dirty = ""; }
    };
    form.addEventListener("input", (event) => {
      changed();
      form.elements.namedItem(`reset__${event.target.name}`)?.remove();
    });
    $$("[data-reset]", form).forEach((button) => button.addEventListener("click", () => {
      const input = form.elements.namedItem(button.dataset.reset);
      if (button.dataset.kind === "bool") input.checked = button.dataset.default.toLowerCase() === "true";
      else input.value = button.dataset.default;
      let marker = form.elements.namedItem(`reset__${input.name}`);
      if (!marker) {
        marker = document.createElement("input");
        marker.type = "hidden";
        marker.name = `reset__${input.name}`;
        form.append(marker);
      }
      marker.value = "true";
      button.closest("[data-field]").querySelector("[data-override-label]").textContent = "保存后使用默认值";
      changed();
    }));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      const button = $("[type=submit]", form);
      button.disabled = true;
      $$(".field-error", form).forEach((el) => { el.hidden = true; });
      $$("[aria-invalid]", form).forEach((el) => el.removeAttribute("aria-invalid"));
      $$("[data-invalid]", form).forEach((el) => el.removeAttribute("data-invalid"));
      try {
        const response = await fetch(form.action, {method: "POST", body: new FormData(form), credentials: "same-origin", headers: {Accept: "application/json"}});
        if (response.redirected && new URL(response.url).pathname.endsWith("/login")) {
          throw new Error("登录已过期。请在新标签页登录后重试，当前输入尚未保存。");
        }
        const data = await response.json();
        if (!response.ok) {
          let first = null;
          for (const [name, message] of Object.entries(data.fields || {})) {
            const input = form.elements.namedItem(name);
            if (!input) continue;
            input.setAttribute("aria-invalid", "true");
            const field = input.closest("[data-field]");
            field.setAttribute("data-invalid", "");
            const slot = field.querySelector(".field-error");
            slot.textContent = message;
            slot.hidden = false;
            const details = field.closest("details");
            if (details) details.open = true;
            first ||= field;
          }
          if (note) note.textContent = `有未保存的修改 · ${Object.keys(data.fields || {}).length || 1} 项需要修正`;
          throw new Error(data.detail || "保存失败，请检查配置。");
        }
        dirty = false;
        location.assign(data.redirect);
      } catch (error) {
        status.hidden = false;
        status.dataset.tone = "danger";
        $("[data-status-message]", status).textContent = error.message;
        $("[data-status-link]", status)?.remove();
        status.focus();
        status.scrollIntoView({block: "center", behavior: "smooth"});
        button.disabled = false;
      }
    });
    window.addEventListener("beforeunload", (event) => {
      if (form.isConnected && dirty) { event.preventDefault(); event.returnValue = ""; }
    });
  }

  /* ================= 日志查看 ================= */
  function initLogViewer(root) {
    if (!once(root, "logReady")) return;
    const output = $("[data-log-content]", root);
    const status = $("[data-log-status]", root);
    const earlier = $("[data-log-earlier]", root);
    const latest = $("[data-log-latest]", root);
    const auto = $("[data-log-auto]", root);
    const follow = $("[data-log-follow]", root);
    const filter = $("[data-log-filter]", root);
    let start = 0;
    let pending = false;
    let text = "";
    const fit = () => {
      const top = output.getBoundingClientRect().top + window.scrollY;
      output.style.height = `${Math.max(320, window.innerHeight - top - 40)}px`;
    };
    function paint() {
      const needle = filter.value.trim();
      if (!needle) { output.textContent = text || "（空文件）"; return; }
      const lower = text.toLowerCase();
      const target = needle.toLowerCase();
      const nodes = [];
      let index = 0;
      let hits = 0;
      for (;;) {
        const found = lower.indexOf(target, index);
        if (found < 0) break;
        nodes.push(document.createTextNode(text.slice(index, found)));
        const mark = document.createElement("mark");
        mark.textContent = text.slice(found, found + needle.length);
        nodes.push(mark);
        index = found + needle.length;
        hits += 1;
      }
      nodes.push(document.createTextNode(text.slice(index)));
      output.replaceChildren(...nodes);
      filter.title = `${hits} 处匹配`;
      output.querySelector("mark")?.scrollIntoView({block: "center"});
    }
    async function refresh(before) {
      if (pending) return;
      pending = true;
      earlier.disabled = latest.disabled = true;
      try {
        const params = new URLSearchParams({file: root.dataset.file});
        if (before !== undefined) params.set("before", before);
        const response = await fetch(`/api/logs/content?${params}`, {cache: "no-store", credentials: "same-origin", signal: AbortSignal.timeout(15000)});
        if (response.status === 401) throw new Error("登录已过期，请重新登录。");
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "读取失败");
        const old = output.scrollTop;
        text = data.text;
        start = data.start;
        paint();
        status.textContent = `字节 ${data.start.toLocaleString()}–${data.end.toLocaleString()} / ${data.size.toLocaleString()} · ${new Date().toLocaleTimeString()} 已刷新${data.start ? " · 窗口起始可能位于一行中间" : ""}`;
        fit();
        if (!filter.value.trim()) {
          output.scrollTop = before === undefined && follow.checked ? output.scrollHeight : (before === undefined ? old : 0);
        }
      } catch (error) {
        status.textContent = `无法刷新：${error.message}（保留上次内容）`;
        auto.checked = false;
      } finally {
        pending = false;
        earlier.disabled = start === 0;
        latest.disabled = false;
      }
    }
    earlier.addEventListener("click", () => { auto.checked = false; refresh(start); });
    latest.addEventListener("click", () => refresh());
    auto.addEventListener("change", () => { if (auto.checked) refresh(); });
    filter.addEventListener("input", paint);
    addEventListener("resize", fit);
    const timer = setInterval(() => {
      if (!root.isConnected) { clearInterval(timer); return; }
      if (auto.checked && !document.hidden) refresh();
    }, 5000);
    fit();
    refresh();
  }

  /* ================= 系统管理 ================= */
  const TERMINAL = new Set(["succeeded", "failed", "cancelled", "interrupted"]);
  const LABELS = {
    pending: "待执行", running: "运行中", paused: "已暂停", draining: "排空中", succeeded: "成功", failed: "失败",
    cancelled: "已取消", interrupted: "执行器失联", active: "运行中", inactive: "已停止", activating: "启动中",
    deactivating: "停止中", failed_unit: "失败", restart_web: "重启 Web", restart_supervisor: "重启 Supervisor",
    restart_all: "全部重启", start_web: "启动 Web", start_supervisor: "启动 Supervisor", start_all: "全部启动",
    stop_web: "停止 Web", stop_supervisor: "停止 Supervisor", stop_all: "全部停止", git_update: "更新代码",
    apply_config: "应用配置",
  };
  const DESCRIPTIONS = {
    restart_web: "重启 Web 服务，页面会短暂不可用。", restart_supervisor: "排空并重启 Supervisor：停止领取新任务，等待正在执行的任务结束后再启动。",
    restart_all: "依次重启 Web 和 Supervisor。", start_web: "启动 Web 服务。", start_supervisor: "启动 Supervisor。",
    stop_web: "停止 Web 服务，停止后需要在服务器上手动启动。", stop_supervisor: "停止 Supervisor：停止领取新任务，等待正在执行的任务结束后退出进程。",
    git_update: "拉取并快进到远端版本，安装依赖、合并配置、升级数据库并恢复各服务原来的运行状态。",
  };
  const TONES = {succeeded: "ok", active: "ok", running: "run", activating: "run", pending: "idle", failed: "danger",
    interrupted: "danger", cancelled: "muted", inactive: "muted", deactivating: "warn", paused: "muted", draining: "warn"};
  const label = (value) => LABELS[value] || value || "—";
  const date = (value) => (value ? new Date(value).toLocaleString("zh-CN") : "—");
  const commit = (hash, message, fallback = "—") => (hash ? (message ? `${message} · ${hash.slice(0, 7)}` : hash.slice(0, 7)) : fallback);
  async function api(path, method = "GET", body) {
    const response = await fetch(path, {
      method, credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf()},
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(path === "/api/system/git/fetch" ? 600000 : 15000),
    });
    if (response.status === 401) { location.assign(loginUrl()); throw new Error("登录已过期"); }
    const value = await response.json();
    if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : JSON.stringify(value));
    return value;
  }
  function showError(root, message) {
    const box = $("[data-system-error]", root);
    if (!box) return;
    box.hidden = !message;
    $("[data-system-error-text]", box).textContent = message || "";
  }
  function pill(element, value) {
    element.textContent = label(value);
    element.dataset.tone = TONES[value] || "idle";
  }
  function cell(row, value, href) {
    const td = row.insertCell();
    if (href) {
      const link = document.createElement("a");
      link.href = href;
      link.textContent = value;
      td.append(link);
    } else {
      td.textContent = value;
    }
    return td;
  }

  function initSystem(root) {
    if (!once(root, "systemReady")) return;
    const dialog = document.getElementById("operation-dialog");
    let pendingKind = null;
    $$("[data-operation]", root).forEach((button) => button.addEventListener("click", () => {
      pendingKind = button.dataset.operation;
      $("h2", dialog).textContent = `${label(pendingKind)}？`;
      $("[data-operation-text]", dialog).textContent = DESCRIPTIONS[pendingKind] || "";
      $("[data-operation-error]", dialog).hidden = true;
      window.uiv3.openDialog(dialog);
    }));
    $("[data-operation-confirm]", dialog).addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const operation = await api("/api/system/operations", "POST", {kind: pendingKind});
        location.assign(`/uiv3/system/operations/${encodeURIComponent(operation.id)}`);
      } catch (error) {
        const slot = $("[data-operation-error]", dialog);
        slot.innerHTML = "";
        const note = document.createElement("div");
        note.className = "note";
        note.dataset.tone = "danger";
        note.textContent = error.message;
        slot.append(note);
        slot.hidden = false;
        button.disabled = false;
      }
    });

    async function refresh() {
      const data = await api("/api/system/status");
      for (const row of $$("[data-service]", root)) {
        const service = data.services[`eharchive-${row.dataset.service}.service`] || {};
        pill($("[data-status]", row), service.ActiveState);
        $("[data-pid]", row).textContent = service.MainPID || "—";
        $("[data-started]", row).textContent = service.ExecMainStartTimestamp || "—";
      }
      $("[data-supervisor-control]", root).textContent = data.database_error || (data.control
        ? `调度状态：${label(data.control.state)} · 心跳：${date(data.control.heartbeat_at)}` : "暂无 Supervisor 心跳");
      const workers = $("[data-workers]", root);
      workers.replaceChildren();
      for (const task of data.modules || []) {
        const row = workers.insertRow();
        cell(row, task.module_label || task.module);
        cell(row, task.manga_name || task.manga_id || "—", task.manga_id ? `/uiv3/archives/${task.manga_id}` : null);
        cell(row, label(task.state));
        cell(row, date(task.started_at));
      }
      if (!workers.rows.length) { const row = workers.insertRow(); const td = cell(row, "暂无运行中的任务"); td.colSpan = 4; td.className = "muted small"; }
      const operations = $("[data-operations]", root);
      operations.replaceChildren();
      for (const operation of data.operations) {
        const row = operations.insertRow();
        const href = `/uiv3/system/operations/${encodeURIComponent(operation.id)}`;
        cell(row, date(operation.created_at), href);
        cell(row, label(operation.kind), href);
        const status = document.createElement("span");
        status.className = "pill";
        pill(status, operation.status);
        row.insertCell().append(status);
        cell(row, operation.phase || "—").className = "mono xs";
      }
      if (!operations.rows.length) { const row = operations.insertRow(); const td = cell(row, "暂无操作记录"); td.colSpan = 4; td.className = "muted small"; }
      const busy = data.operations.some((operation) => !TERMINAL.has(operation.status));
      $$("[data-operation]", root).forEach((button) => {
        button.disabled = busy;
        button.title = busy ? "已有操作正在执行" : "";
      });
      $("[data-system-refreshed]", root).textContent = `${new Date().toLocaleTimeString()} 刷新 · 每 2 秒`;
    }

    function renderGit(value) {
      if (!value) return;
      if (value.schedule) {
        $("[data-git-schedule]", root).textContent = value.schedule.enabled
          ? `每天 ${value.schedule.time} 自动检查（服务器时间），发现更新后手动执行。` : "每日自动检查已关闭。";
      }
      const list = $("[data-git-status]", root);
      list.replaceChildren();
      for (const [name, content] of [
        ["分支", value.branch], ["远端", value.remote],
        ["当前版本", commit(value.old_commit, value.old_commit_message)],
        ["目标版本", commit(value.target_commit, value.target_commit_message, "尚未获取")],
        ["工作区", value.dirty ? "有未提交修改" : "干净"],
        ["更新", value.available ? (value.fast_forward ? "可更新" : "无法快进更新") : "无可用更新"],
        ["上次检查", date(value.check?.checked_at)],
        ["上次结果", value.check?.error || (value.check?.last_success_at ? "成功" : "尚未检查")],
      ]) {
        const term = document.createElement("dt");
        const detail = document.createElement("dd");
        term.textContent = name;
        detail.textContent = content ?? "—";
        if (name === "更新" && value.available) detail.style.color = "var(--ok)";
        list.append(term, detail);
      }
      const changes = $("[data-git-changes]", root);
      changes.textContent = [value.commits, value.files].filter(Boolean).join("\n\n");
      changes.hidden = !value.commits && !value.files;
      $$("[data-update-dot]").forEach((dot) => { dot.hidden = !value.available; });
    }
    document.addEventListener("uiv3:update-status", (event) => { if (root.isConnected) renderGit(event.detail); });
    api("/api/system/git").then(renderGit).catch((error) => showError(root, error.message));
    const fetchButton = $("[data-git-fetch]", root);
    const feedback = $("[data-git-feedback]", root);
    fetchButton.addEventListener("click", async () => {
      const original = fetchButton.innerHTML;
      fetchButton.disabled = true;
      fetchButton.textContent = "正在检查…";
      feedback.hidden = false;
      feedback.textContent = "正在检查更新，请稍候…";
      try {
        const value = await api("/api/system/git/fetch", "POST");
        renderGit(value);
        feedback.textContent = value.available ? (value.fast_forward ? "发现更新。" : "发现更新，但无法快进更新。") : "已是最新。";
      } catch (error) {
        feedback.textContent = `检查失败：${error.name === "TimeoutError" ? "请求超时，请稍后重试。" : error.message || "请求未完成，请稍后重试。"}`;
      } finally {
        fetchButton.disabled = false;
        fetchButton.innerHTML = original;
      }
    });
    (async function poll() {
      if (!root.isConnected) return;
      try { await refresh(); showError(root, ""); } catch (error) { showError(root, error.message); }
      setTimeout(poll, 2000);
    })();
  }

  function initOperation(root) {
    if (!once(root, "operationReady")) return;
    const id = root.dataset.operationDetail;
    const cancel = $("[data-cancel-drain]", root);
    cancel.addEventListener("click", async () => {
      cancel.disabled = true;
      try { await api(`/api/system/operations/${encodeURIComponent(id)}/cancel`, "POST"); toast("已请求取消 drain"); }
      catch (error) { showError(root, error.message); }
      finally { cancel.disabled = false; }
    });
    const text = (selector, value) => {
      const element = $(selector, root);
      if (element) element.textContent = typeof value === "string" ? value : JSON.stringify(value ?? "", null, 2);
    };
    function render(operation) {
      text("[data-op-kind]", label(operation.kind));
      text("[data-op-actor]", operation.actor || "—");
      pill($("[data-op-status]", root), operation.status);
      text("[data-op-phase]", operation.failed_phase || operation.phase || "");
      text("[data-op-commits]", [commit(operation.old_commit, operation.old_commit_message), commit(operation.target_commit, operation.target_commit_message, "尚未获取")].join(" → "));
      text("[data-op-config]", operation.configuration || "无配置发布");
      text("[data-op-events]", operation.events || "");
      text("[data-op-log]", operation.log || "");
      const error = $("[data-op-error]", root);
      error.hidden = !operation.error;
      text("[data-op-error-text]", operation.error || "");
      const glyph = $("[data-op-glyph]", root);
      glyph.dataset.tone = {succeeded: "ok", failed: "danger", interrupted: "danger", running: "run"}[operation.status] || "";
      const listener = operation.configuration?.web_listener;
      if (listener) {
        const address = new URL(location.href);
        address.protocol = "http:";
        if (!["0.0.0.0", "::"].includes(listener.host)) address.hostname = listener.host;
        address.port = listener.port;
        address.pathname = "/uiv3/";
        const link = $("[data-op-address]", root);
        link.href = address.href;
        link.querySelector("span").textContent = `新 Web 地址：${address.href}`;
        link.hidden = false;
      }
      const elapsed = Math.max(0, Math.floor(((operation.finished_at ? Date.parse(operation.finished_at) : Date.now())
        - Date.parse(operation.started_at || operation.created_at)) / 1000));
      text("[data-op-elapsed]", `${elapsed} 秒`);
      cancel.hidden = operation.status !== "running" || operation.phase !== "wait_for_supervisor_exit";
      return TERMINAL.has(operation.status);
    }
    (async function poll() {
      if (!root.isConnected) return;
      try {
        const done = render(await api(`/api/system/operations/${encodeURIComponent(id)}`));
        showError(root, "");
        if (done) return;
      } catch (error) {
        showError(root, error.message);
        try { await api("/health/live"); } catch { showError(root, "Web 连接中断，等待恢复。"); }
      }
      setTimeout(poll, 2000);
    })();
  }

  function init(root) {
    const scope = root.querySelectorAll ? root : document;
    $$("[data-id-picker]", scope).forEach(initPicker);
    $$("[data-risk-form]", scope).forEach(initRisk);
    if (scope.matches?.("[data-risk-form]")) initRisk(scope);
    $$("[data-config-editor]", scope).forEach(initConfig);
    $$("[data-log-viewer]", scope).forEach(initLogViewer);
    $$("[data-system]", scope).forEach(initSystem);
    $$("[data-operation-detail]", scope).forEach(initOperation);
  }
  function boot() { htmx.onLoad(init); }
  if (window.htmx) boot();
  else document.addEventListener("DOMContentLoaded", boot);
})();
