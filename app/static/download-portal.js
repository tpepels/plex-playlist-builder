/* The native downloader is authoritative; this offline view reconnects itself. */
(() => {
  "use strict";
  const status = document.getElementById("engine-status");
  if (!status) return;
  let checking = false;
  let lastPageAttempt = 0;
  async function refresh() {
    if (checking) return;
    checking = true;
    try {
      const reply = await fetch("/api/downloads/bridge/status", {cache:"no-store"});
      const data = await reply.json();
      if (!reply.ok || !data.ok) throw new Error(data.error || "Engine unavailable");
      // The job API can recover before the full native page does. Probe the
      // actual page at a bounded interval and switch only when fully rendered.
      if (Date.now() - lastPageAttempt >= 30000 || !lastPageAttempt) {
        lastPageAttempt = Date.now();
        try {
          const page = await fetch("/downloads", {cache:"no-store",
            headers:{Accept:"text/html"}});
          const markup = await page.text();
          if(page.ok && markup.includes('id="download-form"')) {
            window.location.reload();
            return;
          }
        } catch (_) {
          // Keep the existing offline view; the next scheduled check will retry.
        }
      }
      const n = data.counts || {};
      const active = (n.queued || 0) + (n.running || 0) + (n.finalizing || 0);
      status.textContent = "Downloader responds · " + active + " jobs active. " +
        "Waiting for its full interface; connection will be rechecked automatically.";
      status.classList.remove("error");
    } catch (error) {
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
