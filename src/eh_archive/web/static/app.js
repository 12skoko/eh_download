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
