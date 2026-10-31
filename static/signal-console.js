/* Shared Signal Console theme control. No dependencies or network calls. */
(() => {
  const key = "signal-console-theme-v1";
  const root = document.documentElement;
  const toggle = document.getElementById("signal-theme-toggle");
  if (!toggle) return;
  let current = "cyber";
  try {
    current = localStorage.getItem(key) === "slate" ? "slate" : "cyber";
  } catch (_) { /* Private browsing / disabled storage */ }
  function apply(value) {
    root.dataset.signalTheme = value;
    const mark = document.querySelector(".signal-mark");
    if (mark) {
      const file = value === "cyber" ? "signal-mark.svg" : "signal-mark-static.svg";
      // Only replace the literal filename in this same-origin asset path.
      mark.src = mark.src.replace(/signal-mark(?:-static)?\.svg(?:\?.*)?$/, file);
    }
    toggle.setAttribute("aria-pressed", String(value === "cyber"));
    toggle.textContent = value === "cyber" ? "CYBER: ON" : "CYBER: OFF";
    toggle.title = value === "cyber" ? "Switch to quieter dark theme" : "Enable animated cyber theme";
  }
  apply(current);
  toggle.addEventListener("click", () => {
    current = current === "cyber" ? "slate" : "cyber";
    apply(current);
    try { localStorage.setItem(key, current); } catch (_) {}
  });
})();
