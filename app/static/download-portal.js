/* The native downloader is authoritative; this offline view reconnects itself. */
(() => {
  "use strict";
  const status = document.getElementById("engine-status");
  if (!status) return;
  let hadOutage = false;
  let checking = false;
  async function refresh() {
    if (checking) return;
    checking = true;
    try {
      const reply = await fetch("/api/downloads/bridge/status", {cache:"no-store"});
      const data = await reply.json();
      if (!reply.ok || !data.ok) throw new Error(data.error || "Engine unavailable");
      if (hadOutage) {
        // A genuine offline -> online transition can now serve the native page.
        window.location.reload();
        return;
      }
      const n = data.counts || {};
      const active = (n.queued || 0) + (n.running || 0) + (n.finalizing || 0);
      status.textContent = "Downloader responds · " + active + " jobs active. " +
        "Waiting for its full interface; connection will be rechecked automatically.";
      status.classList.remove("error");
    } catch (error) {
      hadOutage = true;
      status.textContent = "Downloader unavailable: " + error.message +
        " · retrying automatically. Existing jobs are retained.";
      status.classList.add("error");
    } finally {
      checking = false;
    }
  }
  void refresh();
  if (typeof window !== "undefined" && typeof window.setInterval === "function")
    window.setInterval(() => { if (!document.hidden) void refresh(); }, 10000);
})();
