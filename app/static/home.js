/* Progressive, read-only Home overview. Independent backends never block one another. */
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
  function render(states) {
    const items = [];
    let complete = true;
    const done = Object.values(states).filter(x => x !== null).length;
    const plex = states.plex;
    if (plex !== null) {
      if (plex.status === "fulfilled" && plex.value.ok) {
        $("plex-value").textContent = "Connected";
        $("plex-detail").textContent = (plex.value.server || "Plex") + " · " +
          (plex.value.library || "Music");
        badge("plex-indicator", "Online", false);
      } else {
        $("plex-value").textContent = "Offline";
        $("plex-detail").textContent = "Plex connection unavailable";
        badge("plex-indicator", "Offline", true);
        items.push({title:"Plex is unreachable",
          detail:"Check the Plex server or network connection",
          link:"/playlists",action:"Library status",kind:"error"});
        complete = false;
      }
    }
    const playlists = states.playlists;
    if (playlists !== null) {
      if (playlists.status === "fulfilled") {
        const counts = playlists.value.version?.counts || {};
        const total = Number(counts.total ?? 0);
        const pending = Number(counts.needs_sync ?? 0);
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
        items.push({title:"Playlists could not be loaded",
          detail:"Check application storage and Plex",
          link:"/playlists",action:"Open playlists",kind:"error"});
        complete = false;
      }
    }
    const downloads = states.downloads;
    if (downloads !== null) {
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
          items.push({title:"Plex import scan needs attention",
            detail:String(sync.error).slice(0,150),
            link:"/downloads",action:"Review",kind:"warning"});
        } else if (sync.pending) {
          items.push({title:sync.pending + " release(s) awaiting Plex scan",
            detail:"The scan will be retried automatically",
            link:"/downloads",action:"View imports",kind:"neutral"});
        }
      } else {
        $("download-count").textContent = "—";
        $("download-detail").textContent = "Download engine unavailable";
        badge("download-indicator", "Offline", true);
        items.push({title:"Download engine is offline",
          detail:"Check the yt-dlp-server container",
          link:"/downloads",action:"View downloads",kind:"error"});
        complete = false;
      }
    }
    const maintenance = states.maintenance;
    if (maintenance !== null) {
      const line = $("maintenance-status");
      if (maintenance.status === "fulfilled") {
        const data = maintenance.value;
        const verification=data.album_verification||{};
        const verifiedCounts=verification.counts||{};
        const unresolved=["awaiting_index","scan_requested","scan_failed",
          "requires_attention","missing_on_disk","unverifiable",
          "partially_downloaded"].reduce((count,key)=>count+(Number(verifiedCounts[key])||0),0);
        $("unindexed-count").textContent=verification.ok===false?"—":String(unresolved);
        $("unindexed-detail").textContent=!verification.last_checked?
          "Verification pending first scan":
          verification.ok===false?"Verification state unavailable":
          unresolved?"Some imports need investigation or indexing":"All reported published albums verified";
        if(verification.ok===false){
          items.push({title:"Album verification state unavailable",
            detail:String(verification.error||"Unknown error").slice(0,150),
            link:"/downloads-audit",action:"Inspect imports",kind:"error"});
        } else if(unresolved){
          items.push({title:unresolved+" album import(s) need checking",
            detail:(verifiedCounts.requires_attention||0)+" exhausted scan retries · "+
              (verifiedCounts.missing_on_disk||0)+" missing on disk · "+
              (verifiedCounts.awaiting_index||0)+" awaiting Plex",
            link:"/downloads-audit",action:"Diagnose albums",kind:"warning"});
        }
        const drift=data.playlist_drift||{};
        const driftCounts=drift.counts||{};
        const changed=(Number(driftCounts.drift)||0)+(Number(driftCounts.missing)||0)+
          (Number(driftCounts.ambiguous)||0);
        $("drift-count").textContent=drift.ok===false?"—":String(changed);
        $("drift-detail").textContent=!drift.checked_at?
          "Waiting for first live Plex comparison":
          changed?"Saved YAML differs from live Plex":"No detected live playlist differences";
        if(changed)items.push({title:changed+" playlist(s) differ from Plex",
          detail:"Review external edits, missing playlists or changed track order",
          link:"/playlists",action:"Review differences",kind:"warning"});
        if(drift.ok===false)items.push({title:"Live playlist drift monitor unavailable",
          detail:String(drift.error||"State error").slice(0,150),
          link:"/playlists",action:"Review",kind:"error"});
        const coverage=data.audio_coverage||{};
        if(coverage.error)items.push({title:"AudioMuse feature cache cannot be read",
          detail:String(coverage.error).slice(0,150),
          link:"/playlists?work=analysis",action:"Review",kind:"warning"});
        const auto = data.automatic_analysis || {};
        if (!data.enabled) {
          line.textContent = "Background music updates are disabled on the server.";
        } else {
          const jobs = data.jobs || {};
          const audio = jobs.audio || {};
          const library = jobs.library || {};
          const formatAge = stamp => {
            const ms = Date.parse(stamp || "");
            if (!Number.isFinite(ms)) return "not completed yet";
            const minutes = Math.max(0, Math.floor((Date.now() - ms) / 60000));
            return minutes < 1 ? "just now" : minutes < 60 ? minutes + " min ago" :
              Math.floor(minutes / 60) + " h ago";
          };
          line.textContent = "Automatic updates · Audio features " +
            (audio.running ? "updating" : formatAge(audio.last_success)) +
            " · Plex index " + (library.running ? "updating" : formatAge(library.last_success)) +
            (Number(audio.pending) > 0 ? " · " + audio.pending + " cache records queued" : "") +
            (auto.enabled === false ? " · New-music analysis disabled" :
              auto.inflight ? " · Audio analysis running" :
              Number(auto.pending) > 0 ? " · " + auto.pending + " albums awaiting analysis" : "")+
            (coverage.cached!=null?" · AudioMuse "+coverage.cached+
              "/"+(coverage.provider_available??"?")+" cached":"")+
            (Number(coverage.pending)>0?" · "+coverage.pending+" feature records pending":"");
          for (const [job, label, link] of [
            [audio, "Audio feature cache", "/playlists?work=analysis"],
            [library, "Plex music index", "/playlists"],
            [jobs.analysis || {}, "Automatic audio analysis", "/playlists?work=analysis"],
            [auto, "Audio analysis queue", "/playlists?work=analysis"]
          ]) if (job.error) {
            items.push({title:label + " update failed",
              detail:String(job.error).slice(0,150),
              link,action:"View update details",kind:"warning"});
          }
        }
      } else {
        line.textContent = "Automatic update status is unavailable.";
        $("unindexed-count").textContent = "—";
        $("drift-count").textContent = "—";
        $("unindexed-detail").textContent = "Cannot inspect album verification";
        $("drift-detail").textContent = "Cannot inspect playlist drift";
        complete = false;
        items.push({title:"Automatic update status could not be checked",
          detail:"Cannot verify music-cache jobs are running",
          link:"/playlists?work=analysis",action:"View diagnostics",kind:"warning"});
      }
    }
    if (!items.length) {
      items.push({
        title:done < 4 ? "Checking remaining services" : "Nothing requires immediate attention",
        detail:done < 4 ? done + " of 4 services checked" :
          "Downloads and saved playlists have no reported outstanding changes",
        link:done < 4 ? null : "/playlists?work=create",
        action:"Create a playlist",kind:"good"
      });
    }
    $("attention-list").replaceChildren(...items.map(attention));
    $("overview-updated").textContent = done < 4 ? "Checking services · " + done + "/4" :
      complete ? "Live status · just checked" : "Partial status · some services unavailable";
    if (done === 4) {
      running = false;

    }
  }
  async function refresh() {
    if (running) return;
    running = true;
    const states = {plex:null, playlists:null, downloads:null, maintenance:null};
    // Each request renders on arrival. A slow/down Plex server cannot delay the
    // local playlist counts, maintenance state, or downloader queue.
    const requests = [
      ["plex","/api/health"],
      ["playlists","/api/config/summary"],
      ["downloads","/api/downloads/bridge/status"],
      ["maintenance","/api/workspace/maintenance"]
    ].map(([name,path]) =>
      request(path).then(value => { states[name] = {status:"fulfilled",value}; },
                         reason => { states[name] = {status:"rejected",reason}; })
        .finally(() => render(states))
    );
    await Promise.allSettled(requests);
  }
  if (typeof window !== "undefined" && typeof window.addEventListener === "function")
    window.addEventListener("workspace:changed", () => { void refresh(); });
  refresh();
  if (typeof window !== "undefined" && typeof window.setInterval === "function")
    window.setInterval(() => { if (!document.hidden) refresh(); }, 30000);
})();
