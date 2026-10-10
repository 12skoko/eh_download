(() => {
  const root = document.getElementById("log-viewer");
  if (!root || root.dataset.ready) return;
  root.dataset.ready = "true";
  const output = root.querySelector("[data-log-content]");
  const status = root.querySelector("[data-log-status]");
  const earlier = root.querySelector("[data-earlier]");
  const auto = root.querySelector("[data-auto]");
  const latest = root.querySelector("[data-latest]");
  const fitHeight = () => {
    // Use document coordinates so scrolling does not make the viewer grow.
    const top = output.getBoundingClientRect().top + window.scrollY;
    output.style.height = `${Math.max(320, window.innerHeight - top - 48)}px`;
  };
  window.addEventListener("resize", fitHeight);
  fitHeight();
  document.fonts.ready.then(() => { if (root.isConnected) fitHeight(); });
  let start = 0;
  let pending = false;
  async function refresh(before) {
    if (pending) return;
    pending = true;
    earlier.disabled = true;
    latest.disabled = true;
    try {
      const params = new URLSearchParams({file: root.dataset.file});
      if (before !== undefined) params.set("before", before);
      const response = await fetch(`/api/logs/content?${params}`, {
        cache: "no-store", credentials: "same-origin", signal: AbortSignal.timeout(15000),
      });
      if (response.status === 401) throw new Error("登录已过期，请重新登录。");
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "读取失败");
      const oldScroll = output.scrollTop;
      output.textContent = data.text || "（空文件）";
      start = data.start;
      status.textContent = `字节 ${data.start}–${data.end} / ${data.size} · ${new Date().toLocaleTimeString()} 已刷新${data.start ? " · 窗口起始可能位于一行中间" : ""}`;
      fitHeight();
      output.scrollTop = before === undefined && root.querySelector("[data-follow]").checked
        ? output.scrollHeight : (before === undefined ? oldScroll : 0);
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
  const timer = setInterval(() => {
    if (!root.isConnected) { clearInterval(timer); return; }
    if (auto.checked && !document.hidden) refresh();
  }, 5000);
  window.addEventListener("pagehide", () => {
    clearInterval(timer);
    window.removeEventListener("resize", fitHeight);
  }, {once: true});
  refresh();
})();
