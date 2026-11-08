(() => {
  "use strict";
  const status = document.getElementById("engine-status");
  const scan = document.getElementById("scan-plex");
  const result = document.getElementById("scan-result");
  if (!status || !scan || !result) return;

  async function refresh() {
    try {
      const reply = await fetch("/api/downloads/bridge/status", {cache: "no-store"});
      const data = await reply.json();
      if (!reply.ok || !data.ok) {
        throw new Error(data.error || "Engine unavailable");
      }
      const n = data.counts || {};
      const active = (n.queued || 0) + (n.running || 0) + (n.finalizing || 0);
      const action = (n.needs_metadata || 0) + (n.failed || 0) + (n.partial || 0);
      const sync = data.plex_sync || {};
      status.textContent = "Download engine connected · " +
        active + " queued/running · " + action + " need attention" +
        (sync.pending ? " · " + sync.pending + " imported release(s) awaiting Plex scan" : "") +
        (sync.error ? " · Plex scan: " + sync.error : "");
      status.classList.toggle("error", !!sync.error);
    } catch (error) {
      status.textContent = "Download engine unavailable: " + error.message +
        ". Check the yt-dlp-server container. Download history is preserved.";
      status.classList.add("error");
    }
  }

  scan.addEventListener("click", async () => {
    scan.disabled = true;
    result.classList.remove("error");
    result.textContent = "Requesting a Plex Music library scan…";
    try {
      const response = await fetch("/api/download-audit/scan", {
        method: "POST", headers: {"Accept": "application/json"},
      });
      const data = await response.json();
      if (!response.ok || !data.ok) {
        throw new Error(data.detail || "Plex did not accept the scan");
      }
      result.textContent = "Plex accepted the scan. Indexing happens asynchronously. " +
        "Use Review new music once the library has updated.";
    } catch (error) {
      result.classList.add("error");
      result.textContent = "Plex scan failed: " + error.message;
    } finally {
      scan.disabled = false;
      refresh();
    }
  });
  refresh();
  window.setInterval(() => { if (!document.hidden) refresh(); }, 10000);
})();
