// All pages observe local Git state; only the explicit check endpoint fetches remotely.
(() => {
  if (window.ehUpdateStatus) return;
  let current = null;
  let pending = false;
  const publish = value => {
    current = value;
    document.querySelectorAll("[data-update-dot]").forEach(dot => {
      dot.hidden = !value?.available;
    });
    document.dispatchEvent(new CustomEvent("eh:update-status", {detail: value}));
  };
  const refresh = async () => {
    if (pending || document.hidden || !document.querySelector("[data-update-dot]")) return;
    pending = true;
    try {
      const response = await fetch("/api/system/git", {
        credentials: "same-origin", signal: AbortSignal.timeout(15000), cache: "no-store",
      });
      if (response.ok) publish(await response.json());
      else if ([401, 503].includes(response.status)) publish(null);
    } catch { /* Keep the last known result during a restart or connection failure. */ }
    finally { pending = false; }
  };
  window.ehUpdateStatus = {publish, refresh};
  document.addEventListener("htmx:afterSwap", event => {
    if (event.detail.requestConfig?.boosted) { publish(current); refresh(); }
  });
  document.addEventListener("visibilitychange", refresh);
  window.setInterval(refresh, 60000);
  refresh();
})();

// Serialized <dialog open> loses its modal/top-layer state on history restore.
// Keep history snapshots closed and also repair snapshots cached before this fix.
(() => {
  if (window.ehDialogHistoryInstalled) return;
  window.ehDialogHistoryInstalled = true;
  const closeDialogs = () => {
    document.querySelectorAll("dialog[open]").forEach(dialog => dialog.close());
  };
  document.addEventListener("htmx:beforeHistorySave", closeDialogs);
  document.addEventListener("htmx:historyRestore", closeDialogs);
  window.addEventListener("pageshow", event => {
    if (event.persisted) closeDialogs();
  });
})();

document.addEventListener("htmx:configRequest", (event) => {
  const token = document.querySelector('meta[name="csrf-token"]')?.content;
  if (token) event.detail.headers["X-CSRF-Token"] = token;
});

document.addEventListener("htmx:afterSwap", (event) => {
  if (event.detail.requestConfig?.boosted) {
    document.querySelector("#main")?.focus({ preventScroll: true });
  }
});

document.addEventListener("htmx:beforeSwap", (event) => {
  // Do not let a poll that was already in flight replace an open run dialog.
  if (event.detail.target?.classList.contains("module-schedule")
      && event.detail.target.querySelector("dialog[open]")
      && event.detail.requestConfig?.verb === "get") {
    event.detail.shouldSwap = false;
    return;
  }
  // A progress response already in flight must not dismiss the confirmation.
  if (event.detail.target?.id === "direct-download-progress"
      && event.detail.target.querySelector("#cancel-direct-download-dialog[open]")) {
    event.detail.shouldSwap = false;
    return;
  }
  if (event.detail.xhr.status >= 400 && event.detail.xhr.status < 500) {
    event.detail.shouldSwap = true;
    event.detail.isError = false;
    event.detail.target = document.body;
  }
});

document.addEventListener("click", (event) => {
  const opener = event.target.closest("[data-open-dialog]");
  if (opener) {
    const dialog = document.getElementById(opener.dataset.openDialog);
    if (dialog instanceof HTMLDialogElement) {
      if (opener.dataset.copyReason && opener.dataset.copyTarget) {
        const source = document.querySelector(opener.dataset.copyReason);
        const target = dialog.querySelector(opener.dataset.copyTarget);
        if (source instanceof HTMLInputElement && target instanceof HTMLTextAreaElement) {
          target.value = source.value;
        }
      }
      dialog.showModal();
      updateArtifactPath(dialog.querySelector("form"));
    }
    return;
  }

  const closer = event.target.closest("[data-close-dialog]");
  if (closer) closer.closest("dialog")?.close();
});

document.addEventListener("click", (event) => {
  if (event.target instanceof HTMLDialogElement) {
    const bounds = event.target.getBoundingClientRect();
    const inside =
      event.clientX >= bounds.left &&
      event.clientX <= bounds.right &&
      event.clientY >= bounds.top &&
      event.clientY <= bounds.bottom;
    if (!inside) event.target.close();
  }
});

const autoFilterTimers = new WeakMap();

function updateArtifactPath(form) {
  if (!(form instanceof HTMLFormElement)) return;
  const method = form.querySelector("[data-artifact-method]");
  const filename = form.querySelector("[data-artifact-filename]");
  const output = form.querySelector("[data-artifact-path]");
  if (!(method instanceof HTMLSelectElement) ||
      !(filename instanceof HTMLInputElement) ||
      !(output instanceof HTMLOutputElement)) return;

  const directory = method.selectedOptions[0]?.dataset.artifactDirectory || "";
  const cleanFilename = filename.value.trim();
  if (!directory) {
    output.textContent = method.value ? "对应存储目录未配置" : "请选择下载方式并填写文件名";
    return;
  }
  if (!cleanFilename) {
    output.textContent = `${directory}（请填写文件名）`;
    return;
  }
  const separator = directory.includes("\\") && !directory.includes("/") ? "\\" : "/";
  output.textContent = `${directory.replace(/[\\/]+$/, "")}${separator}${cleanFilename}`;
}

document.addEventListener("input", (event) => {
  if (event.target.matches("[data-artifact-filename]")) {
    updateArtifactPath(event.target.closest("form"));
  }
  if (!event.target.matches("[data-debounced-search]")) return;
  const form = event.target.closest("[data-auto-filter-form]");
  if (!(form instanceof HTMLFormElement)) return;
  clearTimeout(autoFilterTimers.get(form));
  autoFilterTimers.set(form, setTimeout(() => form.requestSubmit(), 1000));
});

document.addEventListener("change", (event) => {
  if (event.target.matches("[data-artifact-method]")) {
    updateArtifactPath(event.target.closest("form"));
  }
  if (event.target.matches("[data-debounced-search]")) return;
  const form = event.target.closest("[data-auto-filter-form]");
  if (form instanceof HTMLFormElement) form.requestSubmit();
});

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-artifact-path]").forEach((output) => {
    updateArtifactPath(output.closest("form"));
  });
});
