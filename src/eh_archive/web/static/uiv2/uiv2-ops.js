/* Web v2 workflow forms, configuration and system operations. Local APIs only. */
(() => {
  "use strict";
  if (window.uiv2Ops) return;
  window.uiv2Ops = true;
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const bound = new WeakSet();
  const csrf = () => $('meta[name="csrf-token"]')?.content || "";
  const labels = {pending: "待执行", running: "运行中", paused: "已暂停", draining: "排空中",
    succeeded: "成功", failed: "失败", cancelled: "已取消", interrupted: "执行器失联",
    active: "运行中", inactive: "已停止", activating: "启动中", deactivating: "停止中",
    restart_web: "重启 Web", restart_supervisor: "重启 Supervisor", restart_all: "全部重启",
    start_web: "启动 Web", start_supervisor: "启动 Supervisor", start_all: "全部启动",
    stop_web: "停止 Web", stop_supervisor: "停止 Supervisor", stop_all: "全部停止",
    git_update: "更新代码", apply_config: "应用配置"};
  const label = value => labels[value] || value || "—";
  const date = value => value ? new Date(value).toLocaleString("zh-CN") : "—";
  const commit = (hash, message, fallback = "—") => hash
    ? (message ? message + " · " : "") + hash.slice(0, 7) : fallback;
  const text = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  };
  async function api(path, method = "GET", body) {
    const response = await fetch(path, {method, credentials: "same-origin", cache: "no-store",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf()},
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(path.endsWith("/fetch") ? 600000 : 15000)});
    if (response.status === 401) {
      location.assign("/uiv2/login?next=" + encodeURIComponent(location.pathname + location.search));
      throw new Error("登录已过期");
    }
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "请求失败");
    return data;
  }

  /* Pause database polling while a workflow form has user input. */
  window.uiv2WorkflowCanPoll = () => {
    const panel = $("[data-workflow-panel]");
    return !panel?.querySelector("form[data-v2-dirty]")
      && !panel?.contains(document.activeElement?.closest?.("input, textarea, select"));
  };
  document.addEventListener("input", event => {
    const form = event.target.closest?.("[data-workflow-panel] form");
    if (form) form.dataset.v2Dirty = "true";
  });
  document.addEventListener("change", event => {
    const form = event.target.closest?.("[data-workflow-panel] form");
    if (form) form.dataset.v2Dirty = "true";
    if (form?.matches("[data-video-choices]")) updateRisks(form);
  });
  let workflowTab = "jobs";
  document.addEventListener("htmx:beforeSwap", event => {
    if (event.detail.target?.id === "workflow-panel") {
      workflowTab = $('[data-tab][aria-selected="true"]', event.detail.target)?.dataset.tab || "jobs";
    }
  });
  document.addEventListener("htmx:afterSwap", event => {
    if (event.detail.target?.id === "workflow-panel") {
      $(`[data-tab="${workflowTab}"]`, $("#workflow-panel"))?.click();
    }
  });
  function updateRisks(form) {
    const risks = new Set();
    for (const role of ["image", "video"]) {
      const input = $(`input[name="${role}_choice_id"]:checked`, form);
      for (const risk of JSON.parse(input?.dataset.warnings || "[]")) risks.add(`${role}:${risk}`);
    }
    $$("[data-risk]", form).forEach(box => {
      const needed = risks.has(box.dataset.risk);
      box.hidden = !needed;
      const input = $("input", box);
      input.disabled = !needed;
      input.required = needed;
      if (!needed) input.checked = false;
    });
  }
  const ids = value => [...new Set(value.split(/[\s,，]+/).filter(Boolean))];
  function metadata(form) {
    const input = form.elements.manga_ids;
    const status = $("[data-id-count]", form);
    const update = () => { status.textContent = `已填写 ${ids(input.value).length} 个档案`; };
    input.addEventListener("input", update);
    $("[data-clear-ids]", form).addEventListener("click", () => { input.value = ""; update(); });
    const button = $("[data-mismatch-url]", form);
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const result = await api(button.dataset.mismatchUrl);
        input.value = ids(input.value + "\n" + result.manga_ids.join("\n")).join("\n");
        update();
        status.textContent += ` · 每批上限 ${result.batch_limit}`;
      } catch (error) { status.textContent = error.message; }
      finally { button.disabled = false; }
    });
    update();
  }

  function config(root) {
    const form = $("form", root);
    if (!form) return;
    let dirty = false;
    const status = $("#config-status", root);
    const mark = () => { dirty = true; const note = $("[data-dirty-note]", form);
      note.textContent = "有未保存的修改"; note.dataset.dirty = "true"; };
    form.addEventListener("input", event => {
      mark();
      form.elements.namedItem("reset__" + event.target.name)?.remove();
    });
    $$("[data-reset]", form).forEach(button => button.addEventListener("click", () => {
      const input = form.elements.namedItem(button.dataset.reset);
      if (button.dataset.kind === "bool") input.checked = button.dataset.default.toLowerCase() === "true";
      else input.value = button.dataset.default;
      let marker = form.elements.namedItem("reset__" + input.name);
      if (!marker) { marker = document.createElement("input"); marker.type = "hidden";
        marker.name = "reset__" + input.name; form.append(marker); }
      marker.value = "true";
      $("[data-override-label]", input.closest("[data-field]")).textContent = "保存后使用默认值";
      mark();
    }));
    form.addEventListener("submit", async event => {
      event.preventDefault();
      event.stopPropagation();
      const button = $('[type="submit"]', form);
      button.disabled = true;
      $$("[data-field-error]", form).forEach(el => { el.hidden = true; });
      $$("[aria-invalid]", form).forEach(el => el.removeAttribute("aria-invalid"));
      try {
        const response = await fetch(form.action, {method: "POST", body: new FormData(form),
          credentials: "same-origin", headers: {Accept: "application/json"}});
        if (response.status === 401 || (response.redirected && new URL(response.url).pathname.endsWith("/login")))
          throw new Error("登录已过期。请在新标签页登录后重试，当前输入尚未保存。");
        const data = await response.json();
        if (!response.ok) {
          for (const [name, message] of Object.entries(data.fields || {})) {
            const input = form.elements.namedItem(name);
            if (!input) continue;
            input.setAttribute("aria-invalid", "true");
            const field = input.closest("[data-field]");
            const error = $("[data-field-error]", field);
            error.textContent = message; error.hidden = false;
            const advanced = field.closest("details");
            if (advanced) advanced.open = true;
          }
          throw new Error(data.detail || "保存失败，请检查配置。");
        }
        dirty = false;
        location.assign(data.redirect);
      } catch (error) {
        status.hidden = false; status.dataset.tone = "danger";
        $("[data-status-message]", status).textContent = error.message;
        $$("a", status).forEach(link => link.remove());
        status.focus(); status.scrollIntoView({block: "center"});
        button.disabled = false;
      }
    });
    addEventListener("beforeunload", event => {
      if (root.isConnected && dirty) { event.preventDefault(); event.returnValue = ""; }
    });
  }

  /* System controls use the same /api/system command queue as the old UI. */
  let pendingKind = null;
  let systemBusy = false;
  const terminal = new Set(["succeeded", "failed", "cancelled", "interrupted"]);
  function systemError(message) {
    const slot = $("#system-error");
    if (slot) { slot.hidden = !message; slot.textContent = message; }
  }
  document.addEventListener("click", event => {
    const button = event.target.closest?.("[data-operation]");
    if (button && !button.disabled) {
      pendingKind = button.dataset.operation;
      text("git-feedback", "");
      $("[data-confirm-message]").textContent = `即将${label(pendingKind)}。`;
      window.uiv2.openDialog($("#system-confirm"), button);
    }
  });
  document.addEventListener("submit", async event => {
    if (!event.target.matches?.("[data-system-confirm]")) return;
    event.preventDefault();
    const dialog = event.target.closest("dialog");
    const button = $('[type="submit"]', event.target);
    dialog.dataset.busy = "true"; button.disabled = true;
    try {
      const result = await api("/api/system/operations", "POST", {kind: pendingKind});
      location.assign("/uiv2/system/operations/" + encodeURIComponent(result.id));
    } catch (error) { systemError(error.message); dialog.close(); }
    finally { delete dialog.dataset.busy; button.disabled = false; }
  });
  function cells(tbody, rows) {
    if (!tbody) return;
    tbody.replaceChildren(...rows.map(values => {
      const tr = document.createElement("tr");
      for (const value of values) { const td = document.createElement("td");
        if (value instanceof Node) td.append(value); else td.textContent = value;
        tr.append(td); }
      return tr;
    }));
  }
  function renderGit(value) {
    const list = $("#git-status");
    if (!list) return;
    text("git-check-schedule", value.schedule?.enabled
      ? `每天 ${value.schedule.time} 自动检查（服务器时间），发现更新后手动执行。` : "每日自动检查已关闭。");
    list.replaceChildren();
    for (const [name, content] of [["分支", value.branch], ["远端", value.remote],
      ["当前版本", commit(value.old_commit, value.old_commit_message)],
      ["目标版本", commit(value.target_commit, value.target_commit_message, "尚未获取")],
      ["工作区", value.dirty ? "有未提交修改" : "干净"],
      ["更新", value.available ? (value.fast_forward ? "可更新" : "无法快进更新") : "无可用更新"],
      ["上次检查", date(value.check?.checked_at)],
      ["上次检查结果", value.check?.error || (value.check?.last_success_at ? "成功" : "尚未检查")]]) {
      const dt = document.createElement("dt"), dd = document.createElement("dd");
      dt.textContent = name; dd.textContent = content || "—"; list.append(dt, dd);
    }
    text("git-changes", [value.commits, value.files].filter(Boolean).join("\n\n"));
    $("#git-changes").hidden = !value.commits && !value.files;
    $$("[data-update-dot]").forEach(dot => { dot.hidden = !value.available; });
  }
  document.addEventListener("uiv2:update-status", event => renderGit(event.detail));
  async function dashboard(root) {
    const data = await api("/api/system/status");
    if (!root.isConnected) return;
    $$("[data-service]", root).forEach(row => {
      const service = data.services["eharchive-" + row.dataset.service + ".service"];
      $("[data-status]", row).textContent = label(service.ActiveState);
      $("[data-pid]", row).textContent = service.MainPID;
      $("[data-started]", row).textContent = service.ExecMainStartTimestamp || "—";
    });
    text("supervisor-control", data.database_error || (data.control
      ? `调度状态：${label(data.control.state)} · 心跳：${date(data.control.heartbeat_at)}` : "暂无 Supervisor 心跳"));
    cells($("#system-workers"), (data.modules || []).map(task => [task.module_label || task.module,
      task.manga_name || task.manga_id || "—", label(task.state), date(task.started_at)]));
    if (!$("#system-workers").children.length) cells($("#system-workers"), [["暂无运行中的任务", "", "", ""]]);
    cells($("#operation-history"), data.operations.map(operation => {
      const link = document.createElement("a");
      link.href = "/uiv2/system/operations/" + encodeURIComponent(operation.id);
      link.textContent = label(operation.kind);
      return [date(operation.created_at), link, label(operation.status), operation.phase];
    }));
    const busy = data.operations.some(operation => !terminal.has(operation.status));
    $$("[data-operation]", root).forEach(button => { button.disabled = busy; });
  }
  async function operation(root) {
    const data = await api("/api/system/operations/" + encodeURIComponent(root.dataset.v2Operation));
    if (!root.isConnected) return;
    text("operation-kind", label(data.kind)); text("operation-actor", data.actor);
    text("operation-status", label(data.status)); text("operation-phase", data.failed_phase || data.phase);
    const pill = $(".page-head .pill", root);
    if (pill) {
      pill.textContent = label(data.status); pill.title = data.status;
      pill.dataset.tone = {pending: "idle", running: "run", succeeded: "ok", failed: "danger",
        interrupted: "danger", cancelled: "muted"}[data.status] || "idle";
    }
    text("operation-error", data.error || "");
    text("operation-commits", commit(data.old_commit, data.old_commit_message) + " → "
      + commit(data.target_commit, data.target_commit_message, "尚未获取"));
    text("operation-configuration", data.configuration || "");
    text("operation-events", data.events || ""); text("operation-log", data.log || "");
    const elapsed = Math.max(0, Math.floor(((data.finished_at ? Date.parse(data.finished_at) : Date.now())
      - Date.parse(data.started_at || data.created_at)) / 1000));
    text("operation-elapsed", elapsed + " 秒");
    $("#cancel-drain").hidden = data.status !== "running" || data.phase !== "wait_for_supervisor_exit";
    const listener = data.configuration?.web_listener;
    if (listener) {
      const address = new URL(location.href); address.protocol = "http:";
      if (!["0.0.0.0", "::"].includes(listener.host)) address.hostname = listener.host;
      address.port = listener.port;
      const link = $("#operation-new-address"); link.href = address.href;
      link.textContent = address.href; link.hidden = false;
    }
    if (terminal.has(data.status)) root.dataset.done = "true";
  }
  async function pollSystem() {
    const root = $("[data-v2-system], [data-v2-operation]");
    if (!root || root.dataset.done || systemBusy || !window.uiv2CanPoll()) return;
    systemBusy = true;
    try { if (root.dataset.v2Operation) await operation(root); else await dashboard(root);
      systemError(""); }
    catch (error) { systemError("读取失败，等待连接恢复：" + error.message); }
    finally { systemBusy = false; }
  }
  function init() {
    $$("[data-video-choices]").forEach(updateRisks);
    for (const root of $$("[data-v2-metadata], [data-v2-config], [data-v2-system], [data-v2-operation]")) {
      if (bound.has(root)) continue;
      bound.add(root);
      if (root.matches("[data-v2-metadata]")) metadata(root);
      if (root.matches("[data-v2-config]")) config(root);
      if (root.matches("[data-v2-system]")) {
        api("/api/system/git").then(renderGit).catch(error => systemError(error.message));
        $("#git-fetch", root).addEventListener("click", async event => {
          const button = event.target; button.disabled = true;
          text("git-feedback", "正在检查更新，请稍候…");
          try { const result = await api("/api/system/git/fetch", "POST"); renderGit(result);
            text("git-feedback", result.available ? (result.fast_forward ? "发现更新。" : "发现更新，但无法快进。") : "已是最新。"); }
          catch (error) { text("git-feedback", "检查失败：" + error.message); }
          finally { button.disabled = false; }
        });
      }
      if (root.matches("[data-v2-operation]")) $("#cancel-drain", root).addEventListener("click", async event => {
        const button = event.target; button.disabled = true;
        try { await api("/api/system/operations/" + encodeURIComponent(root.dataset.v2Operation) + "/cancel", "POST");
          delete root.dataset.done; await pollSystem(); }
        catch (error) { systemError(error.message); }
        finally { button.disabled = false; }
      });
    }
    pollSystem();
  }
  setInterval(pollSystem, 2000);
  document.addEventListener("visibilitychange", pollSystem);
  document.addEventListener("htmx:load", init);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
