/* EH Archive 控制台 v2 —— 全局交互（主题、对话框、弹层、通知、HTMX 反馈、搜索、列表工作区、批量操作）。 */
(() => {
  "use strict";
  if (window.uiv2) return;
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content || "";
  const typing = (target) => target instanceof HTMLElement
    && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
  const sprite = (name) => `<svg class="icon" aria-hidden="true"><use href="/static/uiv2/icons.svg#i-${name}"/></svg>`;

  /* ---------------- 主题 ---------------- */
  const THEME_LABELS = {auto: "主题：跟随系统", light: "主题：浅色", dark: "主题：深色"};
  const media = matchMedia("(prefers-color-scheme: dark)");
  function applyTheme(mode) {
    const dark = mode === "dark" || (mode === "auto" && media.matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    document.documentElement.dataset.themeMode = mode;
    $$("[data-theme-label]").forEach((el) => { el.textContent = THEME_LABELS[mode]; });
  }
  const currentTheme = () => localStorage.getItem("uiv2-theme") || "auto";
  media.addEventListener("change", () => { if (currentTheme() === "auto") applyTheme("auto"); });

  /* ---------------- 通知 ---------------- */
  function toast(message, tone = "ok") {
    const host = $("#toasts");
    if (!host || !message) return;
    const item = document.createElement("div");
    item.className = "toast";
    item.dataset.tone = tone;
    item.setAttribute("role", tone === "danger" ? "alert" : "status");
    item.innerHTML = sprite(tone === "danger" ? "circle-alert" : tone === "warn" ? "triangle-alert" : "circle-check");
    const text = document.createElement("div");
    text.className = "grow";
    text.textContent = message;
    item.append(text);
    item.addEventListener("click", () => item.remove());
    host.append(item);
    setTimeout(() => item.remove(), tone === "danger" ? 9000 : 4500);
  }
  function initialToasts(root) {
    $$("template[data-initial-toast]", root).forEach((template) => {
      toast(template.content.textContent.trim(), template.dataset.tone || "ok");
      template.remove();
    });
    const url = new URL(location.href);
    if (url.searchParams.has("toast")) {
      url.searchParams.delete("toast");
      history.replaceState(history.state, "", url);
    }
  }
  document.addEventListener("uiv2:toast", (event) => toast(event.detail?.message, event.detail?.tone));

  /* ---------------- 弹层与对话框 ---------------- */
  const openPopovers = () => { try { return $$(":popover-open"); } catch { return []; } };
  const closePopovers = () => openPopovers().forEach((pop) => pop.hidePopover());
  const holdsOverlay = (element) => {
    if (!(element instanceof Element)) return false;
    try { return Boolean(element.matches(":popover-open") || element.querySelector("dialog[open], :popover-open")); }
    catch { return Boolean(element.querySelector("dialog[open]")); }
  };
  window.uiv2CanPoll = () => !document.hidden && !document.querySelector("dialog[open], [data-pane-resizing]") && !openPopovers().length;

  const invokers = {};
  document.addEventListener("click", (event) => {
    const invoker = event.target.closest?.("[popovertarget]");
    if (invoker) invokers[invoker.getAttribute("popovertarget")] = invoker;
  }, true);
  function positionPopover(pop) {
    const invoker = invokers[pop.id] || document.querySelector(`[popovertarget="${CSS.escape(pop.id)}"]`);
    if (!invoker) return;
    const rect = invoker.getBoundingClientRect();
    const toolbar = invoker.closest(".list-toolbar");
    const anchorBottom = toolbar && pop.classList.contains("filter-pop")
      ? Math.max(rect.bottom, toolbar.getBoundingClientRect().bottom) : rect.bottom;
    if (toolbar && pop.classList.contains("filter-pop")) {
      pop.style.maxHeight = `${Math.max(0, innerHeight - anchorBottom - 18)}px`;
    }
    const width = pop.offsetWidth;
    const height = pop.offsetHeight;
    let left = Math.min(rect.left, innerWidth - width - 12);
    let top = anchorBottom + 6;
    if (top + height > innerHeight - 12) top = Math.max(12, rect.top - height - 6);
    left = Math.max(12, left);
    pop.style.left = `${left}px`;
    pop.style.top = `${top}px`;
  }
  document.addEventListener("toggle", (event) => {
    const pop = event.target;
    if (pop instanceof HTMLElement && pop.hasAttribute("popover") && event.newState === "open") {
      positionPopover(pop);
      pop.dispatchEvent(new CustomEvent("uiv2:popover-open", {bubbles: true}));
    }
  }, true);
  addEventListener("resize", () => openPopovers().forEach(positionPopover));
  document.addEventListener("scroll", () => openPopovers().forEach(positionPopover), true);

  function clearErrors(scope) {
    $$("[data-form-error]", scope).forEach((slot) => { slot.hidden = true; slot.innerHTML = ""; });
  }
  function openDialog(dialog, opener) {
    if (!(dialog instanceof HTMLDialogElement)) return;
    closePopovers();
    clearErrors(dialog);
    if (!dialog.open) dialog.showModal();
    dialog.dispatchEvent(new CustomEvent("uiv2:dialog-open", {bubbles: true, detail: {opener}}));
  }
  document.addEventListener("click", (event) => {
    const opener = event.target.closest?.("[data-open-dialog]");
    if (opener && !opener.disabled) {
      event.preventDefault();
      openDialog(document.getElementById(opener.dataset.openDialog), opener);
      return;
    }
    const closer = event.target.closest?.("[data-close-dialog]");
    if (closer) {
      const dialog = closer.closest("dialog");
      if (dialog && !dialog.dataset.busy) dialog.close();
      return;
    }
    if (event.target instanceof HTMLDialogElement && !event.target.dataset.busy) {
      const box = event.target.getBoundingClientRect();
      const inside = event.clientX >= box.left && event.clientX <= box.right
        && event.clientY >= box.top && event.clientY <= box.bottom;
      if (!inside) event.target.close();
    }
  });
  document.addEventListener("cancel", (event) => {
    if (event.target instanceof HTMLDialogElement && event.target.dataset.busy) event.preventDefault();
  }, true);
  // 历史快照中序列化的 <dialog open> 会丢失模态状态，保存前统一关闭。
  document.addEventListener("htmx:beforeHistorySave", () => {
    $$("dialog[open]").forEach((dialog) => dialog.close());
    closePopovers();
  });

  /* ---------------- HTMX 请求反馈 ---------------- */
  const errorSlot = (element) => {
    if (!(element instanceof Element)) return null;
    const form = element.closest("form") || element;
    return form.querySelector("[data-form-error]")
      || element.closest("[data-error-scope]")?.querySelector("[data-form-error]")
      || element.closest("dialog")?.querySelector("[data-form-error]");
  };
  const plainText = (value) => {
    const holder = document.createElement("div");
    holder.innerHTML = value;
    return holder.textContent.replace(/\s+/g, " ").trim().slice(0, 300);
  };
  document.addEventListener("htmx:configRequest", (event) => {
    event.detail.headers["X-CSRF-Token"] = csrf();
  });
  document.addEventListener("htmx:beforeRequest", (event) => {
    const slot = errorSlot(event.detail.elt);
    if (slot && event.detail.requestConfig?.verb !== "get") { slot.hidden = true; slot.innerHTML = ""; }
  });
  document.addEventListener("htmx:beforeSwap", (event) => {
    const {xhr, requestConfig} = event.detail;
    const verb = requestConfig?.verb;
    if (xhr.status === 401) {
      event.detail.shouldSwap = false;
      location.assign(xhr.getResponseHeader("HX-Redirect")
        || `/uiv2/login?next=${encodeURIComponent(location.pathname + location.search)}`);
      return;
    }
    if (xhr.status >= 400) {
      event.detail.shouldSwap = false;
      event.detail.isError = false;
      const slot = errorSlot(requestConfig?.elt);
      if (slot && verb !== "get") {
        slot.innerHTML = xhr.responseText;
        slot.hidden = false;
        slot.scrollIntoView({block: "nearest"});
      } else {
        toast(plainText(xhr.responseText) || `请求失败（HTTP ${xhr.status}）`, "danger");
      }
      return;
    }
    if (verb === "get") {
      if (event.detail.target && holdsOverlay(event.detail.target) && !requestConfig?.elt?.closest?.("dialog")) {
        event.detail.shouldSwap = false;
      }
      return;
    }
    // 成功的写操作：先关闭发起它的对话框和弹层，再替换内容。
    requestConfig?.elt?.closest?.("dialog")?.close();
    closePopovers();
  });
  document.addEventListener("htmx:oobBeforeSwap", (event) => {
    if (holdsOverlay(event.detail.target)) event.detail.shouldSwap = false;
  });
  document.addEventListener("htmx:sendError", () => toast("无法连接 Web 服务，请检查网络后重试", "danger"));
  document.addEventListener("htmx:timeout", () => toast("请求超时，请稍后重试", "danger"));
  document.addEventListener("htmx:afterSwap", (event) => {
    if (event.detail.requestConfig?.boosted) {
      document.querySelector("[data-app]")?.removeAttribute("data-nav-open");
      $("#main")?.focus({preventScroll: true});
    }
  });
  document.addEventListener("click", (event) => {
    const button = event.target.closest?.("[data-refresh-region]");
    if (!button) return;
    const region = button.closest("[data-refresh-url]");
    const dialog = button.closest("dialog");
    if (dialog) dialog.close();
    if (region) htmx.ajax("GET", region.dataset.refreshUrl, {target: region, swap: "outerHTML"});
    else location.reload();
  });

  /* ---------------- 导航抽屉、主题按钮、复制 ---------------- */
  document.addEventListener("click", (event) => {
    const app = document.querySelector("[data-app]");
    if (event.target.closest?.("[data-nav-toggle]")) app?.toggleAttribute("data-nav-open");
    else if (event.target.closest?.("[data-nav-close]")) app?.removeAttribute("data-nav-open");
    if (event.target.closest?.("[data-theme-toggle]")) {
      const order = ["auto", "light", "dark"];
      const next = order[(order.indexOf(currentTheme()) + 1) % order.length];
      localStorage.setItem("uiv2-theme", next);
      applyTheme(next);
    }
    const copy = event.target.closest?.("[data-copy]");
    if (copy) {
      navigator.clipboard?.writeText(copy.dataset.copy).then(() => toast("已复制"), () => toast("复制失败", "danger"));
    }
  });

  /* ---------------- 全局搜索 ---------------- */
  function openPalette() {
    const palette = $("#palette");
    if (!palette) return;
    openDialog(palette);
    const input = $("[data-palette-input]", palette);
    input.select();
    input.focus();
    filterPalette(palette);
  }
  function paletteItems(palette) { return $$(".palette-item", palette).filter((item) => !item.hidden); }
  function filterPalette(palette) {
    const query = $("[data-palette-input]", palette).value.trim().toLowerCase();
    $$("[data-palette-page]", palette).forEach((item) => {
      item.hidden = Boolean(query) && !item.dataset.words.toLowerCase().includes(query);
    });
    setActive(palette, 0);
  }
  function setActive(palette, index) {
    const items = paletteItems(palette);
    items.forEach((item, position) => item.toggleAttribute("data-active", position === index));
    palette.dataset.active = String(index);
    items[index]?.scrollIntoView({block: "nearest"});
  }
  document.addEventListener("click", (event) => {
    if (event.target.closest?.("[data-open-palette]")) openPalette();
    if (event.target.closest?.(".palette-item")) event.target.closest("dialog")?.close();
  });
  document.addEventListener("input", (event) => {
    if (event.target.matches?.("[data-palette-input]")) filterPalette(event.target.closest("dialog"));
  });
  document.addEventListener("htmx:afterSwap", (event) => {
    if (event.detail.target?.id === "palette-results") setActive($("#palette"), 0);
  });
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      openPalette();
      return;
    }
    const palette = $("#palette");
    if (palette?.open && ["ArrowDown", "ArrowUp", "Enter"].includes(event.key)) {
      const items = paletteItems(palette);
      const index = Number(palette.dataset.active || 0);
      if (event.key === "Enter") {
        if (items[index]) { event.preventDefault(); items[index].click(); }
        return;
      }
      event.preventDefault();
      setActive(palette, Math.max(0, Math.min(items.length - 1, index + (event.key === "ArrowDown" ? 1 : -1))));
      return;
    }
    if (event.key === "/" && !typing(event.target) && !document.querySelector("dialog[open]")) {
      event.preventDefault();
      openPalette();
    }
  });

  /* ---------------- 自动筛选表单 ---------------- */
  const timers = new WeakMap();
  document.addEventListener("input", (event) => {
    const form = event.target.closest?.("form[data-auto-filter]");
    if (!form || !event.target.matches("[data-debounce]")) return;
    clearTimeout(timers.get(form));
    timers.set(form, setTimeout(() => form.requestSubmit(), 1000));
  });
  document.addEventListener("change", (event) => {
    const form = event.target.form || event.target.closest?.("form");
    if (!(form instanceof HTMLFormElement) || !form.matches("[data-auto-filter]")) return;
    if (event.target.matches("[data-debounce]")) return;
    form.requestSubmit();
  });
  document.addEventListener("submit", (event) => {
    if (event.target.matches?.("form[data-auto-filter]")) {
      clearTimeout(timers.get(event.target));
      updateChips(event.target);
    }
  }, true);
  document.addEventListener("click", (event) => {
    const button = event.target.closest?.("[data-clear]");
    const form = button && (button.form || button.closest("form"));
    if (!form) return;
    const names = button.dataset.clear.split(",");
    for (const element of form.elements) {
      if (!names.includes(element.name)) continue;
      if (element.type === "checkbox") element.checked = false;
      else if (element.type === "radio") element.checked = element.value === "";
      else element.value = "";
    }
    form.requestSubmit();
  });
  function updateChips(root) {
    $$("[data-chip]", root.ownerDocument === document ? document : root).forEach((chip) => {
      const form = document.getElementById(chip.dataset.chipForm) || chip.closest("form");
      if (!form) return;
      const names = chip.dataset.chip.split(",");
      const values = [];
      for (const name of names) {
        for (const element of form.elements) {
          if (element.name !== name) continue;
          if ((element.type === "checkbox" || element.type === "radio") && !element.checked) continue;
          const value = element.value.trim();
          if (!value) continue;
          const label = element.dataset.label || element.selectedOptions?.[0]?.textContent || value;
          values.push(label.trim());
        }
      }
      const slot = chip.querySelector("[data-chip-value]");
      chip.toggleAttribute("data-active", values.length > 0);
      if (slot) slot.textContent = values.length > 1 ? `${values[0]} +${values.length - 1}` : values[0] || "";
    });
  }

  /* ---------------- 归档路径预览 ---------------- */
  function updateArtifactPath(form) {
    if (!(form instanceof HTMLFormElement)) return;
    const method = form.querySelector("[data-artifact-method]");
    const filename = form.querySelector("[data-artifact-filename]");
    const output = form.querySelector("[data-artifact-path]");
    if (!method || !filename || !output) return;
    const directory = method.selectedOptions[0]?.dataset.artifactDirectory || "";
    const clean = filename.value.trim();
    if (!directory) { output.textContent = method.value ? "对应存储目录未配置" : "请选择下载方式并填写文件名"; return; }
    if (!clean) { output.textContent = `${directory}（请填写文件名）`; return; }
    const separator = directory.includes("\\") && !directory.includes("/") ? "\\" : "/";
    output.textContent = `${directory.replace(/[\\/]+$/, "")}${separator}${clean}`;
  }
  document.addEventListener("input", (event) => {
    if (event.target.matches?.("[data-artifact-filename]")) updateArtifactPath(event.target.form);
  });
  document.addEventListener("change", (event) => {
    if (event.target.matches?.("[data-artifact-method]")) updateArtifactPath(event.target.form);
  });

  /* ---------------- 状态变更对话框 ---------------- */
  function selectStatusTarget(dialog, value) {
    $$("[data-status-target]", dialog).forEach((button) => {
      button.setAttribute("aria-selected", String(button.dataset.statusTarget === value));
    });
    $$("[data-status-form]", dialog).forEach((form) => {
      form.hidden = form.dataset.statusForm !== value;
      if (!form.hidden) updateArtifactPath(form);
    });
    const empty = $("[data-status-empty]", dialog);
    if (empty) empty.hidden = Boolean(value);
  }
  document.addEventListener("click", (event) => {
    const button = event.target.closest?.("[data-status-target]");
    if (button && !button.disabled) selectStatusTarget(button.closest("dialog"), button.dataset.statusTarget);
  });
  document.addEventListener("uiv2:dialog-open", (event) => {
    const dialog = event.target;
    if (dialog.matches("[data-status-dialog]")) {
      const wanted = event.detail?.opener?.dataset.target;
      selectStatusTarget(dialog, wanted || "");
    }
    $$("form", dialog).forEach(updateArtifactPath);
  });

  /* ---------------- 列表工作区与详情窗格 ---------------- */
  const workspace = () => document.querySelector("[data-workspace]");
  const itemsOf = (ws) => $$("[data-item]", ws);
  const autoNext = () => localStorage.getItem("uiv2-auto-next") !== "false";
  let paneRatio = null;
  try {
    const saved = Number(localStorage.getItem("uiv2-pane-ratio"));
    if (saved > 0 && saved < 1) paneRatio = saved;
  } catch { /* Storage can be disabled; resizing still works for this page. */ }
  function savePaneRatio(value) {
    paneRatio = value;
    try {
      if (value === null) localStorage.removeItem("uiv2-pane-ratio");
      else localStorage.setItem("uiv2-pane-ratio", String(value));
    } catch { /* Keep the in-memory preference. */ }
  }
  function sizePane(ws, preferred) {
    if (!ws) return null;
    if (matchMedia("(max-width: 1100px)").matches) {
      ws.style.removeProperty("--pane-w");
      return null;
    }
    const available = ws.clientWidth - ($(".facets", ws)?.getBoundingClientRect().width || 0);
    const maximum = Math.max(0, available - 320);
    const minimum = Math.min(420, maximum);
    const wanted = preferred ?? (paneRatio === null ? Math.min(1000, available * .46) : available * paneRatio);
    const width = Math.round(Math.max(minimum, Math.min(maximum, wanted)));
    ws.style.setProperty("--pane-w", `${width}px`);
    const handle = $(".pane-resizer", ws);
    if (handle) {
      handle.setAttribute("aria-valuemin", String(Math.round(minimum)));
      handle.setAttribute("aria-valuemax", String(Math.round(maximum)));
      handle.setAttribute("aria-valuenow", String(width));
      handle.setAttribute("aria-valuetext", `${width} 像素`);
    }
    return {width, available, minimum, maximum};
  }
  function initPaneResize(ws) {
    const pane = $("[data-pane]", ws);
    if (!pane) return;
    if (!$(".pane-resizer", pane)) {
      const handle = document.createElement("div");
      handle.className = "pane-resizer";
      handle.tabIndex = 0;
      handle.setAttribute("role", "separator");
      handle.setAttribute("aria-orientation", "vertical");
      handle.setAttribute("aria-controls", "pane-body");
      handle.setAttribute("aria-label", "调整档案详情宽度");
      handle.title = "拖动调整宽度；双击恢复自动宽度；方向键微调";
      pane.prepend(handle);
      let drag = null;
      handle.addEventListener("pointerdown", event => {
        if (event.button !== 0) return;
        const layout = sizePane(ws);
        if (!layout) return;
        event.preventDefault();
        handle.focus({preventScroll: true});
        drag = {x: event.clientX, width: layout.width, moved: false};
        ws.setAttribute("data-pane-resizing", "");
        handle.setPointerCapture(event.pointerId);
      });
      handle.addEventListener("pointermove", event => {
        if (!drag) return;
        drag.moved ||= event.clientX !== drag.x;
        sizePane(ws, drag.width + drag.x - event.clientX);
      });
      handle.addEventListener("pointerup", () => {
        if (!drag) return;
        if (drag.moved) {
          const width = Number(handle.getAttribute("aria-valuenow"));
          const layout = sizePane(ws, width);
          if (layout) savePaneRatio(layout.width / layout.available);
        }
        drag = null;
        ws.removeAttribute("data-pane-resizing");
      });
      const cancel = () => {
        if (!drag) return;
        drag = null;
        ws.removeAttribute("data-pane-resizing");
        sizePane(ws);
      };
      handle.addEventListener("pointercancel", cancel);
      handle.addEventListener("lostpointercapture", cancel);
      handle.addEventListener("dblclick", () => { savePaneRatio(null); sizePane(ws); });
      handle.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        const layout = sizePane(ws);
        if (!layout) return;
        event.preventDefault(); event.stopPropagation();
        const step = event.shiftKey ? 160 : 40;
        const wanted = event.key === "Home" ? layout.minimum : event.key === "End" ? layout.maximum
          : layout.width + (event.key === "ArrowLeft" ? step : -step);
        const resized = sizePane(ws, wanted);
        savePaneRatio(resized.width / resized.available);
      });
    }
    sizePane(ws);
  }
  addEventListener("resize", () => sizePane(workspace()));
  function markSelected(ws) {
    const current = ws.dataset.open || "";
    itemsOf(ws).forEach((item) => item.setAttribute("aria-selected", String(item.dataset.mangaId === current)));
  }
  function openPane(id, {focusItem = false} = {}) {
    const ws = workspace();
    const pane = ws && $("[data-pane]", ws);
    if (!pane || !id) return;
    ws.dataset.open = id;
    ws.classList.add("with-pane");
    pane.hidden = false;
    sizePane(ws);
    pane.removeAttribute("data-empty");
    markSelected(ws);
    const item = itemsOf(ws).find((element) => element.dataset.mangaId === id);
    if (focusItem) item?.scrollIntoView({block: "nearest"});
    htmx.ajax("GET", `/uiv2/pane/${id.split("/").map(encodeURIComponent).join("/")}`, {target: "#pane-body", swap: "innerHTML"});
    const url = new URL(location.href);
    url.searchParams.set("open", id);
    history.replaceState(history.state, "", url);
  }
  let paneEmptyHTML = "";
  function closePane() {
    const ws = workspace();
    if (!ws) return;
    delete ws.dataset.open;
    const pane = $("[data-pane]", ws);
    if (ws.hasAttribute("data-pane-persistent")) {
      const body = $("#pane-body", ws);
      if (body) body.innerHTML = paneEmptyHTML;
      pane?.setAttribute("data-empty", "");
    } else {
      ws.classList.remove("with-pane");
      if (pane) pane.hidden = true;
    }
    markSelected(ws);
    const url = new URL(location.href);
    url.searchParams.delete("open");
    history.replaceState(history.state, "", url);
  }
  document.addEventListener("click", (event) => {
    const ws = workspace();
    if (!ws) return;
    if (event.target.closest?.("[data-pane-close]")) { closePane(); return; }
    const item = event.target.closest?.("[data-item]");
    if (!item || !ws.contains(item)) return;
    const link = event.target.closest("a");
    if (link && (event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0)) return;
    if (event.target.closest("input, label, button, select, textarea, summary")) return;
    if (link && !link.matches("[data-pane-link]")) return;
    event.preventDefault();
    openPane(item.dataset.mangaId);
  });
  document.addEventListener("keydown", (event) => {
    const ws = workspace();
    if (!ws || typing(event.target) || event.ctrlKey || event.metaKey || document.querySelector("dialog[open]")) return;
    if (event.key === "Escape" && ws.dataset.open) { closePane(); return; }
    const move = {j: 1, ArrowDown: 1, k: -1, ArrowUp: -1}[event.key];
    if (!move) return;
    const items = itemsOf(ws);
    if (!items.length) return;
    event.preventDefault();
    const index = items.findIndex((item) => item.dataset.mangaId === ws.dataset.open);
    const next = items[index < 0 ? 0 : Math.max(0, Math.min(items.length - 1, index + move))];
    openPane(next.dataset.mangaId, {focusItem: true});
  });
  document.addEventListener("change", (event) => {
    if (event.target.matches?.("[data-auto-next]")) localStorage.setItem("uiv2-auto-next", String(event.target.checked));
  });
  function refreshList(ws) {
    const list = ws && document.getElementById(ws.dataset.list);
    if (!list) return Promise.resolve();
    return htmx.ajax("GET", location.href, {target: list, swap: "outerHTML", headers: {"HX-Target": list.id}});
  }
  document.addEventListener("uiv2:changed", (event) => {
    const ws = workspace();
    if (!ws) return;
    const {manga_id: id, advance} = event.detail || {};
    const items = itemsOf(ws);
    const index = items.findIndex((item) => item.dataset.mangaId === id);
    const neighbour = index >= 0 ? (items[index + 1] || items[index - 1]) : null;
    const nextId = advance && autoNext() && ws.dataset.open === id ? neighbour?.dataset.mangaId : null;
    refreshList(ws).then(() => {
      if (nextId) openPane(nextId, {focusItem: true});
      else markSelected(ws);
    });
  });
  document.addEventListener("uiv2:list-refresh", () => refreshList(workspace()).then(() => {
    const ws = workspace();
    if (ws) markSelected(ws);
  }));

  /* ---------------- 批量修改状态 ---------------- */
  let bulkBusy = false;
  const bulkScope = () => document.querySelector("[data-bulk-scope]");
  const bulkSelected = () => $$("[data-bulk-item]:checked", bulkScope() || document);
  function updateBulk() {
    const scope = bulkScope();
    if (!scope) return;
    const count = bulkSelected().length;
    const total = $$("[data-bulk-item]", scope).length;
    const bar = document.querySelector("[data-bulkbar]");
    if (bar) {
      bar.hidden = count === 0;
      $$("[data-bulk-count]").forEach((el) => { el.textContent = String(count); });
    }
    $$("[data-bulk-all]", scope).forEach((all) => {
      all.checked = total > 0 && count === total;
      all.indeterminate = count > 0 && count < total;
      all.disabled = total === 0;
    });
  }
  function updateBulkFields(form) {
    const target = form.elements.target_status;
    const option = target.selectedOptions[0];
    target.dataset.tone = option?.dataset.tone || "body";
    const needsMethod = target.value === "download_pending";
    const needsReplacement = target.value === "outdated";
    form.querySelector("[data-bulk-method]").hidden = !needsMethod;
    form.elements.download_method.disabled = !needsMethod;
    form.elements.download_method.required = needsMethod;
    form.querySelector("[data-bulk-replacement]").hidden = !needsReplacement;
    form.elements.superseded_by_id.disabled = !needsReplacement;
    form.elements.superseded_by_id.required = needsReplacement;
    const needsReason = option?.dataset.requiresReason === "yes";
    const defaultReason = option?.dataset.defaultReason || "";
    const reason = form.elements.reason;
    if (!reason.value.trim() || reason.value === reason.dataset.defaultReason) reason.value = defaultReason;
    reason.dataset.defaultReason = defaultReason;
    reason.required = needsReason;
    form.querySelector("[data-bulk-reason-label]").textContent = needsReason ? "公共原因（必填）" : "公共原因（可选）";
    form.querySelector("[data-bulk-description]").textContent = option?.dataset.description || "";
  }
  document.addEventListener("change", (event) => {
    if (event.target.matches?.("[data-bulk-all]")) {
      $$("[data-bulk-item]", bulkScope()).forEach((item) => { item.checked = event.target.checked; });
    }
    if (event.target.matches?.("[data-bulk-all], [data-bulk-item]")) updateBulk();
    if (event.target.matches?.("[data-bulk-target]")) updateBulkFields(event.target.form);
  });
  document.addEventListener("click", (event) => {
    if (event.target.closest?.("[data-bulk-clear]")) {
      bulkSelected().forEach((item) => { item.checked = false; });
      updateBulk();
    }
    if (event.target.closest?.("[data-bulk-open]")) {
      const dialog = document.getElementById("bulk-dialog");
      const form = $("[data-bulk-form]", dialog);
      $("[data-bulk-dialog-count]", dialog).textContent = String(bulkSelected().length);
      $("[data-bulk-results]", dialog).hidden = true;
      $("[data-bulk-fields]", dialog).hidden = false;
      $$("[data-bulk-phase=edit]", dialog).forEach((el) => { el.hidden = false; });
      $$("[data-bulk-phase=done]", dialog).forEach((el) => { el.hidden = true; });
      $("[data-bulk-message]", dialog).textContent = "";
      updateBulkFields(form);
      openDialog(dialog);
    }
  });
  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!form.matches?.("[data-bulk-form]")) return;
    event.preventDefault();
    if (bulkBusy) return;
    const dialog = form.closest("dialog");
    const message = $("[data-bulk-message]", dialog);
    const items = bulkSelected().map((item) => [item.value, Number(item.dataset.version)]);
    if (!items.length) { message.textContent = "请先勾选档案。"; return; }
    if (!form.reportValidity()) return;
    const payload = {
      items, target_status: form.elements.target_status.value, reason: form.elements.reason.value,
      download_method: form.elements.download_method.disabled ? null : form.elements.download_method.value,
      superseded_by_id: form.elements.superseded_by_id.disabled ? null : form.elements.superseded_by_id.value.trim(),
    };
    bulkBusy = true;
    dialog.dataset.busy = "true";
    $$("button", form).forEach((button) => { button.disabled = true; });
    message.textContent = "正在逐条处理，请稍候…";
    try {
      const response = await fetch("/api/bulk-status", {
        method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf()},
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) {
        message.textContent = typeof data.detail === "string" ? data.detail : "提交参数无效，请检查后重试。";
        return;
      }
      const counts = {success: 0, skipped: 0, failed: 0};
      data.results.forEach((item) => { counts[item.outcome] += 1; });
      const labels = {success: "成功", skipped: "跳过", failed: "失败"};
      const results = $("[data-bulk-results]", dialog);
      $("[data-bulk-summary]", results).textContent = `成功 ${counts.success} 条，跳过 ${counts.skipped} 条，失败 ${counts.failed} 条。`;
      const list = $("[data-bulk-list]", results);
      list.replaceChildren(...data.results.map((item) => {
        const row = document.createElement("li");
        row.dataset.outcome = item.outcome;
        const id = document.createElement("span");
        id.className = "id";
        id.textContent = item.manga_id;
        const outcome = document.createElement("span");
        outcome.className = "pill";
        outcome.dataset.tone = {success: "ok", skipped: "muted", failed: "danger"}[item.outcome];
        outcome.textContent = labels[item.outcome];
        const text = document.createElement("span");
        text.className = "xs muted grow";
        text.textContent = item.message;
        row.append(outcome, id, text);
        return row;
      }));
      results.hidden = false;
      $("[data-bulk-fields]", dialog).hidden = true;
      $$("[data-bulk-phase=edit]", dialog).forEach((el) => { el.hidden = true; });
      $$("[data-bulk-phase=done]", dialog).forEach((el) => { el.hidden = false; });
      message.textContent = "";
      try { await refreshList(workspace()); message.textContent = "列表已刷新，选择已清空。"; }
      catch { message.textContent = "列表刷新失败，请手动刷新后再操作。"; }
      updateBulk();
    } catch {
      message.textContent = "未能取得操作结果，部分档案可能已修改。请关闭弹窗并刷新核对后再操作。";
    } finally {
      bulkBusy = false;
      delete dialog.dataset.busy;
      $$("button", form).forEach((button) => { button.disabled = false; });
    }
  });

  /* ---------------- 标签页 ---------------- */
  document.addEventListener("click", (event) => {
    const tab = event.target.closest?.("[data-tab]");
    const root = tab?.closest("[data-tabs]");
    if (!root) return;
    const name = tab.dataset.tab;
    $$("[data-tab]", root).filter((el) => el.closest("[data-tabs]") === root)
      .forEach((el) => el.setAttribute("aria-selected", String(el.dataset.tab === name)));
    $$("[data-tab-panel]", root).filter((el) => el.closest("[data-tabs]") === root)
      .forEach((el) => { el.hidden = el.dataset.tabPanel !== name; });
  });

  /* ---------------- 按需加载状态计数 ---------------- */
  document.addEventListener("uiv2:popover-open", async (event) => {
    const pop = event.target;
    const url = pop.dataset?.countsUrl;
    if (!url || pop.dataset.countsLoaded) return;
    pop.dataset.countsLoaded = "true";
    try {
      const response = await fetch(url, {credentials: "same-origin", headers: {Accept: "application/json"}});
      const counts = await response.json();
      $$("[data-count-for]", pop).forEach((slot) => {
        slot.textContent = (counts[slot.dataset.countFor] || 0).toLocaleString("en-US");
      });
    } catch { delete pop.dataset.countsLoaded; }
  });

  /* ---------------- 更新提示点（与旧版一致，只读本地 Git 状态） ---------------- */
  let updatePending = false;
  async function refreshUpdateDot() {
    if (updatePending || document.hidden || !document.querySelector("[data-update-dot]")) return;
    updatePending = true;
    try {
      const response = await fetch("/api/system/git", {credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(15000)});
      if (response.ok) {
        const value = await response.json();
        $$("[data-update-dot]").forEach((dot) => { dot.hidden = !value?.available; });
        document.dispatchEvent(new CustomEvent("uiv2:update-status", {detail: value}));
      } else if ([401, 503].includes(response.status)) {
        $$("[data-update-dot]").forEach((dot) => { dot.hidden = true; });
      }
    } catch { /* 重启或断线时保留上一次结果 */ }
    finally { updatePending = false; }
  }
  setInterval(refreshUpdateDot, 60000);
  document.addEventListener("visibilitychange", refreshUpdateDot);

  /* ---------------- 初始化 ---------------- */
  function init(root) {
    applyTheme(currentTheme());
    initialToasts(root);
    $$("[data-auto-next]", root.querySelectorAll ? root : document).forEach((el) => { el.checked = autoNext(); });
    updateChips(root);
    updateBulk();
    const ws = workspace();
    if (ws) {
      initPaneResize(ws);
      const empty = $("[data-pane-empty]", ws);
      if (empty && root.contains?.(empty)) {
        paneEmptyHTML = empty.outerHTML;
        $("[data-pane]", ws)?.setAttribute("data-empty", "");
      }
      const open = new URL(location.href).searchParams.get("open");
      if (open && ws.dataset.open !== open && root.contains?.(ws)) openPane(open);
      else markSelected(ws);
    }
  }
  function boot() {
    htmx.onLoad((root) => init(root));
    refreshUpdateDot();
  }
  if (window.htmx) boot();
  else document.addEventListener("DOMContentLoaded", boot);

  window.uiv2 = {toast, openDialog, openPane, closePane, refreshList, closePopovers};
})();
