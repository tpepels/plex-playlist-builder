/* One persisted appearance preference for the entire music workspace. */
(() => {
  "use strict";
  const key = "signal-console-theme-v1";
  const themes = new Set(["cyber", "slate", "terminal"]);
  const root = document.documentElement;
  const select = document.getElementById("signal-theme-toggle");
  if (!select) return;

  let current = "cyber";
  try {
    const stored = localStorage.getItem(key);
    if (themes.has(stored)) current = stored;
  } catch (_) { /* Storage may be disabled; keep the default. */ }

  function apply(value) {
    if (!themes.has(value)) return;
    current = value;
    root.dataset.signalTheme = value;
    select.value = value;
    const mark = document.querySelector(".signal-mark");
    if (mark) {
      const name = value === "cyber" ? "signal-mark.svg" : "signal-mark-static.svg";
      mark.src = mark.src.replace(/signal-mark(?:-static)?\.svg(?:\?.*)?$/, name);
    }
    // The header, all pages and the diagnostics inherit the same theme
    // without a reload or a parallel navigation control.
    try { localStorage.setItem(key, current); } catch (_) {}
  }
  apply(current);
  select.addEventListener("change", () => apply(select.value));
})();
