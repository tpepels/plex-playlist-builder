/* Read-only landing-page status. Existing queues/playlists are never mutated. */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const timeoutMs = 8000;
  const request = async path => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(path, {
        cache: "no-store", signal: controller.signal,
        headers: {Accept: "application/json"}
      });
      if (!response.ok) throw new Error("HTTP " + response.status);
      return await response.json();
    } finally {
      clearTimeout(timer);
    }
  };
  function badge(id, value, offline) {
    const el = $(id);
    el.textContent = value;
    el.classList.toggle("offline", !!offline);
    el.classList.toggle("loading", false);
  }
  function attention({title, detail, link, action, kind}) {
    const row = document.createElement("div");
    row.className = "attention-row";
    const mark = document.createElement("span");
    mark.className = "attention-mark " + (kind || "neutral");
    mark.setAttribute("aria-hidden", "true");
    const body = document.createElement("div");
    const bold = document.createElement("strong");
    bold.textContent = title;
    const small = document.createElement("small");
    small.textContent = detail;
    body.append(bold, small);
    row.append(mark, body);
    if (link) {
      const anchor = document.createElement("a");
      anchor.href = link;
      anchor.textContent = action || "Review";
      row.append(anchor);
    }
    return row;
  }
  let running = false;
  async function refresh() {
    if (running) return;
    running = true;
    $("refresh-home").disabled = true;
    $("overview-updated").textContent = "Checking services…";
    const [plex, playlists, downloads] = await Promise.allSettled([
      request("/api/health"),
      request("/api/config"),
      request("/api/downloads/bridge/status")
    ]);
    const items = [];
    let complete = true;

    if (plex.status === "fulfilled" && plex.value.ok) {
      $("plex-value").textContent = "Connected";
      $("plex-detail").textContent = (plex.value.server || "Plex") +
        " · " + (plex.value.library || "Music");
      badge("plex-indicator", "Online", false);
    } else {
      $("plex-value").textContent = "Offline";
      $("plex-detail").textContent = "Plex connection unavailable";
      badge("plex-indicator", "Offline", true);
      items.push({title:"Plex is unreachable",detail:"Check the Plex server or network connection",
        link:"/playlists",action:"Library status",kind:"error"});
      complete = false;
    }

    if (playlists.status === "fulfilled") {
      const data = playlists.value;
      const counts = data.version?.counts || {};
      const total = Number(counts.total ?? (data.playlists || []).length);
      const pending = Number(counts.needs_sync ?? (data.pending || []).length);
      $("playlist-count").textContent = Number.isFinite(total) ? String(total) : "—";
      $("playlist-detail").textContent = "Playlist definitions in the library";
      $("sync-count").textContent = Number.isFinite(pending) ? String(pending) : "—";
      $("sync-detail").textContent = pending === 0
        ? "No pending playlist changes" : "Review before syncing to Plex";
      if (pending > 0) items.push({
        title:pending + " playlist" + (pending === 1 ? "" : "s") + " awaiting sync",
        detail:"Review the differences before publishing",
        link:"/playlists?pending=1",action:"Review changes",kind:"warning"
      });
    } else {
      $("playlist-count").textContent = "—";
      $("playlist-detail").textContent = "Cannot load playlist definitions";
      $("sync-count").textContent = "—";
      $("sync-detail").textContent = "Playlist state unavailable";
      items.push({title:"Playlists could not be loaded",detail:"Check application storage and Plex",
        link:"/playlists",action:"Open playlists",kind:"error"});
      complete = false;
    }

    if (downloads.status === "fulfilled" && downloads.value.ok) {
      const counts = downloads.value.counts || {};
      const active = Number(counts.queued || 0) + Number(counts.running || 0) +
        Number(counts.finalizing || 0);
      const errors = Number(counts.failed || 0) + Number(counts.partial || 0) +
        Number(counts.needs_metadata || 0);
      $("download-count").textContent = String(active);
      $("download-detail").textContent = active ? "Items queued or processing" :
        "Download queue is idle";
      badge("download-indicator", "Online", false);
      if (errors) items.push({
        title:errors + " download" + (errors === 1 ? "" : "s") + " need attention",
        detail:"Review failed, partial or metadata-pending jobs",
        link:"/downloads",action:"Resolve",kind:"warning"
      });
      const sync = downloads.value.plex_sync || {};
      if (sync.error) {
        items.push({title:"Plex import scan needs attention",detail:String(sync.error).slice(0,150),
          link:"/downloads",action:"Review",kind:"warning"});
      } else if (sync.pending) {
        items.push({title:sync.pending + " release(s) awaiting Plex scan",
          detail:"The scan will be retried automatically",link:"/downloads",
          action:"View imports",kind:"neutral"});
      }
    } else {
      $("download-count").textContent = "—";
      $("download-detail").textContent = "Download engine unavailable";
      badge("download-indicator", "Offline", true);
      items.push({title:"Download engine is offline",detail:"Check the yt-dlp-server container",
        link:"/downloads",action:"View downloads",kind:"error"});
      complete = false;
    }
    if (!items.length) items.push({
      title:"Nothing requires immediate attention",
      detail:"Downloads and saved playlists have no reported outstanding changes",
      link:"/playlist-generator",action:"Create a playlist",kind:"good"
    });
    $("attention-list").replaceChildren(...items.map(attention));
    $("overview-updated").textContent = complete ? "Live status · just checked" :
      "Partial status · some services unavailable";
    running = false;
    $("refresh-home").disabled = false;
  }
  $("refresh-home").addEventListener("click", refresh);
  refresh();
  window.setInterval(() => { if (!document.hidden) refresh(); }, 30000);
})();
