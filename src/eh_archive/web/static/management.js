(() => {
  const root = document.querySelector("[data-system-dashboard], [data-operation-detail], .config-sections");
  if (!root || root.dataset.initialized) return;
  root.dataset.initialized = "true";
  let error = document.getElementById("system-error");
  if (!error) {
    error = document.createElement("div");
    error.id = "system-error";
    error.className = "alert warning";
    error.setAttribute("role", "alert");
    error.hidden = true;
    root.before(error);
  }
  const terminal = new Set(["succeeded", "failed", "cancelled", "interrupted"]);
  const labels = {pending: "待执行", running: "运行中", paused: "已暂停", draining: "排空中",
    succeeded: "成功", failed: "失败", cancelled: "已取消", interrupted: "执行器失联",
    active: "运行中", inactive: "已停止", activating: "启动中", deactivating: "停止中",
    restart_web: "重启 Web", restart_supervisor: "重启 Supervisor", restart_all: "全部重启",
    start_web: "启动 Web", start_supervisor: "启动 Supervisor", start_all: "全部启动",
    stop_web: "停止 Web", stop_supervisor: "停止 Supervisor", stop_all: "全部停止",
    git_update: "更新代码", apply_config: "应用配置"};
  const label = value => labels[value] || value || "-";
  const date = value => value ? new Date(value).toLocaleString("zh-CN") : "-";
  const commitLabel = (hash, message, fallback = "-") => {
    if (!hash) return fallback;
    const shortHash = hash.slice(0, 7);
    return message ? message + " · " + shortHash : shortHash;
  };
  let identifier = root.dataset.operationDetail;
  let cancel;
  let polling = false;
  const text = (id, value) => {
    const element = document.getElementById(id);
    if (element) element.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  };
  const showError = (message) => {
    error.hidden = !message;
    error.textContent = message;
  };
  async function api(path, method = "GET", body) {
    const response = await fetch(path, {
      method, credentials: "same-origin",
      headers: {"Content-Type": "application/json",
        "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content},
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(path === "/api/system/git/fetch" ? 600000 : 15000),
    });
    if (response.status === 401) {
      window.location.assign("/login?next=" + encodeURIComponent(window.location.pathname));
      throw new Error("登录已过期");
    }
    const value = await response.json();
    if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : JSON.stringify(value));
    return value;
  }
  root.querySelectorAll("[data-operation]").forEach(button => {
    button.addEventListener("click", async () => {
      if (!window.confirm(button.textContent + "？")) return;
      button.disabled = true;
      try {
        const operation = await api("/api/system/operations", "POST", {kind: button.dataset.operation});
        displayOperation(operation);
      } catch (exc) {
        showError(exc.message);
        button.disabled = false;
      }
    });
  });
  async function refreshDashboard() {
    const data = await api("/api/system/status");
    if (identifier || !root.isConnected) return;
    for (const row of root.querySelectorAll("[data-service]")) {
      const service = data.services["eharchive-" + row.dataset.service + ".service"];
      row.querySelector("[data-status]").textContent = label(service.ActiveState);
      row.querySelector("[data-pid]").textContent = service.MainPID;
      row.querySelector("[data-started]").textContent = service.ExecMainStartTimestamp;
    }
    text("supervisor-control", data.database_error || (data.control
      ? "调度状态：" + label(data.control.state) + " · 心跳：" + date(data.control.heartbeat_at)
      : "暂无 Supervisor 心跳"));
    const workers = document.getElementById("system-workers");
    workers.replaceChildren();
    for (const task of data.modules || []) {
      const row = document.createElement("tr");
      for (const value of [task.module_label || task.module, task.manga_name || task.manga_id || "-",
        label(task.state), date(task.started_at)]) {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.append(cell);
      }
      workers.append(row);
    }
    if (!workers.children.length) {
      const row = workers.insertRow();
      const cell = row.insertCell();
      cell.colSpan = 4;
      cell.textContent = "暂无运行中的任务";
    }
    const tbody = document.getElementById("operation-history");
    tbody.replaceChildren();
    for (const operation of data.operations) {
      const row = document.createElement("tr");
      for (const value of [date(operation.created_at), label(operation.kind), label(operation.status), operation.phase]) {
        const cell = document.createElement("td");
        const link = document.createElement("a");
        link.href = "/system/operations/" + encodeURIComponent(operation.id);
        link.textContent = value;
        cell.append(link);
        row.append(cell);
      }
      tbody.append(row);
    }
    const busy = data.operations.some(operation => !terminal.has(operation.status));
    root.querySelectorAll("[data-operation]").forEach(button => { button.disabled = busy; });
  }
  function bindCancel() {
    cancel = document.getElementById("cancel-drain");
    if (cancel) cancel.addEventListener("click", async () => {
    cancel.disabled = true;
    try { await api("/api/system/operations/" + identifier + "/cancel", "POST"); }
    catch (exc) { showError(exc.message); }
    finally { cancel.disabled = false; }
    });
  }
  bindCancel();
  function displayOperation(operation) {
    identifier = operation.id;
    root.replaceChildren(document.getElementById("system-operation-shell").content.cloneNode(true));
    root.dataset.operationDetail = identifier;
    root.classList.remove("config-sections");
    document.querySelector(".page-head h1").textContent = "操作详情";
    window.history.replaceState(null, "", "/system/operations/" + identifier);
    bindCancel();
    renderOperation(operation);
    if (!polling) poll();
  }
  root.querySelectorAll('form[action^="/config/"]').forEach(form => {
    form.addEventListener("submit", async event => {
      event.preventDefault();
      event.stopPropagation();
      const button = form.querySelector('button[type="submit"]');
      button.disabled = true;
      try {
        const response = await fetch(form.action, {
          method: "POST", body: new FormData(form), headers: {Accept: "application/json"},
          credentials: "same-origin",
        });
        if (!response.ok) {
          const contentType = response.headers.get("content-type") || "";
          if (contentType.includes("application/json")) {
            throw new Error((await response.json()).detail);
          }
          const page = new DOMParser().parseFromString(await response.text(), "text/html");
          throw new Error(page.querySelector("main")?.textContent.trim() || "配置提交失败");
        }
        displayOperation(await response.json());
      } catch (exc) { showError(exc.message); button.disabled = false; }
    });
  });
  async function refreshDetail() {
    const operation = await api("/api/system/operations/" + identifier);
    renderOperation(operation);
    return terminal.has(operation.status);
  }
  function renderOperation(operation) {
    text("operation-kind", label(operation.kind));
    text("operation-actor", operation.actor);
    text("operation-status", label(operation.status));
    text("operation-phase", operation.failed_phase || operation.phase);
    text("operation-error", operation.error || "");
    const oldCommit = commitLabel(operation.old_commit, operation.old_commit_message);
    const targetCommit = commitLabel(operation.target_commit, operation.target_commit_message, "尚未获取");
    text("operation-commits", [oldCommit, targetCommit].filter(Boolean).join(" → "));
    text("operation-configuration", operation.configuration || "");
    text("operation-events", operation.events || "");
    text("operation-log", operation.log || "");
    const listener = operation.configuration?.web_listener;
    if (listener) {
      const address = new URL(window.location.href);
      address.protocol = "http:";
      if (!["0.0.0.0", "::"].includes(listener.host)) address.hostname = listener.host;
      address.port = listener.port;
      const link = document.getElementById("operation-new-address");
      link.href = address.href;
      link.textContent = address.href;
      link.hidden = false;
    }
    const elapsed = Math.max(0, Math.floor(((operation.finished_at ? Date.parse(operation.finished_at) : Date.now())
      - Date.parse(operation.started_at || operation.created_at)) / 1000));
    text("operation-elapsed", elapsed + " 秒");
    cancel.hidden = operation.status !== "running" || operation.phase !== "wait_for_supervisor_exit";
  }
  const fetchButton = document.getElementById("git-fetch");
  function renderGit(value) {
    const list = document.getElementById("git-status");
    if (!list) return;
    if (!value) return;
    if (value.schedule) text("git-check-schedule", value.schedule.enabled
      ? "每天 " + value.schedule.time + " 自动检查（服务器时间），发现更新后手动执行。"
      : "每日自动检查已关闭。");
    list.replaceChildren();
    for (const [name, content] of [["分支", value.branch], ["远端", value.remote],
      ["当前版本", commitLabel(value.old_commit, value.old_commit_message)],
      ["目标版本", commitLabel(value.target_commit, value.target_commit_message, "尚未获取")],
      ["工作区", value.dirty ? "有未提交修改" : "干净"],
      ["更新", value.available ? (value.fast_forward ? "可更新" : "无法快进更新") : "无可用更新"],
      ["上次检查", date(value.check?.checked_at)],
      ["上次检查结果", value.check?.error || (value.check?.last_success_at ? "成功" : "尚未检查")]]) {
      const term = document.createElement("dt");
      const detail = document.createElement("dd");
      term.textContent = name;
      detail.textContent = content;
      list.append(term, detail);
    }
    text("git-changes", [value.commits, value.files].filter(Boolean).join("\n\n"));
    document.getElementById("git-changes").hidden = !value.commits && !value.files;
  }
  if (fetchButton) {
    const onUpdate = event => {
      if (!root.isConnected) document.removeEventListener("eh:update-status", onUpdate);
      else renderGit(event.detail);
    };
    document.addEventListener("eh:update-status", onUpdate);
    api("/api/system/git").then(value => window.ehUpdateStatus.publish(value))
      .catch(exc => showError(exc.message));
    fetchButton.addEventListener("click", async () => {
      fetchButton.disabled = true;
      try { window.ehUpdateStatus.publish(await api("/api/system/git/fetch", "POST")); }
      catch (exc) { showError(exc.message); window.ehUpdateStatus.refresh(); }
      finally { fetchButton.disabled = false; }
    });
  }
  async function poll() {
    if (!root.isConnected) return;
    polling = true;
    try {
      const done = identifier ? await refreshDetail() : (await refreshDashboard(), false);
      showError("");
      if (done) { polling = false; return; }
    } catch (exc) {
      showError(exc.message);
      if (identifier) {
        try { await api("/health/live"); }
        catch { showError("Web 连接中断，等待恢复。"); }
      }
    }
    window.setTimeout(poll, 2000);
  }
  if (identifier || root.hasAttribute("data-system-dashboard")) poll();
})();
