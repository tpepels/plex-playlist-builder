// End-to-end browser flow tests with a minimal DOM and mocked API responses.
// Run: node --test tests/test_import_ui.mjs
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const html = fs.readFileSync(new URL("../app/templates/index.html", import.meta.url), "utf8");
const script = html.split("<script>", 2)[1]?.split("</script>", 1)[0];
assert.ok(script, "Inline browser JavaScript must exist");

function fakeElement() {
  return {
    value: "", textContent: "", innerHTML: "", className: "", hidden: false,
    disabled: false, checked: false, dataset: {}, style: {}, children: [],
    classList: { toggle() {} },
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    addEventListener(name, callback) { this["on" + name] = callback; },
    setAttribute(name, value) { this[name] = value; },
    querySelectorAll() { return []; },
  };
}

function makeHarness({ invalid = false, alreadyLoaded = false, syncFails = false, albumFound = true } = {}) {
  const elements = new Map();
  const rows = [];
  const cards = fakeElement();
  cards.querySelectorAll = (query) => query === ".playlist" ? rows : [];
  cards.appendChild = (row) => rows.push(row);
  elements.set("cards", cards);

  function $(id) {
    if (!elements.has(id)) elements.set(id, fakeElement());
    return elements.get(id);
  }

  const tabs = ["playlists", "health", "duplicates", "newmusic", "isc", "discover"].map(view => {
    const e = fakeElement();
    e.dataset.view = view;
    return e;
  });
  $("insightDays").value = "30";
  $("duplicateKind").value = "all";
  $("iscStatus").value = "missing";
  $("iscMood").value = "all";
  const document = {
    getElementById: $,
    querySelectorAll(query) {
      if (query === ".view-tab") return tabs;
      if (query === ".build") return rows.map((row) => row.querySelector(".build"));
      return [];
    },
    createElement() {
      const row = fakeElement();
      const children = new Map();
      row.querySelector = (query) => {
        if (!children.has(query)) {
          const child = fakeElement();
          child.closest = () => row;
          children.set(query, child);
        }
        return children.get(query);
      };
      return row;
    },
  };

  let imported = false;
  let synced = false;
  const calls = [];
  const requests = [];
  const filename = "new-playlists.yml";

  function snapshot() {
    const alpha = { items: [{ rating_key: "1" }] };
    const beta = { items: [{ rating_key: "2" }] };
    const definitions = imported && !alreadyLoaded ? { Alpha: alpha, Beta: beta } : { Alpha: alpha };
    const statuses = imported && !alreadyLoaded
      ? { Alpha: "synced", Beta: synced ? "synced" : "needs_sync" }
      : { Alpha: "synced" };
    const counts = {
      total: Object.keys(definitions).length,
      needs_sync: Object.values(statuses).filter((x) => x === "needs_sync").length,
      synced: Object.values(statuses).filter((x) => x === "synced").length,
      unverified: 0,
    };
    return {
      text: "playlists: {}\n", definitions, playlists: Object.keys(definitions),
      version: { id: imported ? "new-id" : "old-id", last_import: null, counts, sync_statuses: statuses },
    };
  }

  async function fetch(url, options={}) {
    calls.push(url);
    requests.push({url, options});
    let body = {};
    let status = 200;
    if (url === "/api/health") {
      body = { server: "Test Plex", library: "Music" };
    } else if (url === "/api/config") {
      body = snapshot();
    } else if (url.startsWith("/api/insights?")) {
      body = {
        summary: {
          library_tracks: 4, unlisted_tracks: 2, in_multiple_playlists: 1,
          missing_references: 0, rotating_playlists: 0, uncurated_recent_albums: 0,
        },
        playlists: [], recent_albums: [],
      };
    } else if (url.startsWith("/api/discover/isc?")) {
      body = {
        source:"In Sheep's Clothing Hi-Fi",
        source_url:"https://insheepsclothinghifi.com/collection/",
        fetched_at:"2026-10-26T18:00:00+00:00",
        sources_scanned:7, stale:false, warnings:[],
        summary:{total:3,missing:1,owned:1,edition:1,
                 source_categories:["ambient","japanese-jazz-funk"]},
        albums:[
          {artist:"Hiroshi Suzuki",album:"Cat",year:1975,status:"missing",
           source_url:"https://insheepsclothinghifi.com/album/hiroshi-suzuki-cat/",
           source_styles:["japan"],site_styles:["Jazz","Funk"],site_tags:["Jazz-Funk"],
           moods:["japanese-jazz-funk"],plex_matches:[]},
          {artist:"The Blue Nile",album:"Hats",year:1989,status:"edition",
           source_url:"https://insheepsclothinghifi.com/album/blue-nile-hats/",
           source_styles:["pop"],site_styles:["Pop"],site_tags:[],
           moods:[],plex_matches:[{artist:"The Blue Nile",album:"Hats (Remastered)",
                                  album_rating_key:"a",tracks:7}]},
          {artist:"Paul Bley",album:"Open, to Love",year:1973,status:"owned",
           source_url:"https://insheepsclothinghifi.com/album/paul-bley-open-to-love/",
           source_styles:["jazz"],site_styles:["Jazz","Ambient"],site_tags:[],
           moods:["ambient","ecm-jazz"],plex_matches:[{artist:"Paul Bley",
                                album:"Open, to Love",album_rating_key:"b",tracks:7}]}
        ]
      };
    } else if (url.startsWith("/api/duplicates?")) {
      const sampleDetails = [{disc:1,track:1,title:"My Foolish Heart",duration_ms:300000,rating_key:"1"}];
      body = {
        summary: {
          library_tracks:6,album_groups:2,full_album_groups:1,
          edition_groups:1,partial_groups:0,folder_review_candidates:2,
          tracks_without_media_path:0,
        },
        groups: [
          {kind:"full",artist:"Bill Evans",album:"Waltz for Debby",total_copies:2,reviewable_folders:2,
           releases:[
             {id:1,album:"Waltz for Debby",directory:"/music/Bill Evans/Original",
              folder_status:"Dedicated album folder in Plex - check files manually",
              folder_review_candidate:true,match:"full",tracks:1,matched_tracks:1,
              album_rating_key:"a",track_rating_keys:["1"],track_details:sampleDetails},
             {id:2,album:"Waltz for Debby",directory:"/music/Bill Evans/Duplicate",
              folder_status:"Dedicated album folder in Plex - check files manually",
              folder_review_candidate:true,match:"full",tracks:1,matched_tracks:1,
              album_rating_key:"b",track_rating_keys:["2"],track_details:sampleDetails}]},
          {kind:"edition",artist:"Bill Evans",album:"You Must Believe in Spring",
           total_copies:2,reviewable_folders:0,
           releases:[
             {id:3,album:"You Must Believe in Spring",directory:"/music/Bill Evans/Spring",
              folder_status:"Review",folder_review_candidate:false,match:"edition",
              tracks:1,matched_tracks:1,album_rating_key:"c",track_rating_keys:["3"],track_details:sampleDetails},
             {id:4,album:"You Must Believe in Spring (2022 Remaster)",
              directory:"/music/Bill Evans/Spring 2022",folder_status:"Review",
              folder_review_candidate:false,match:"edition",tracks:1,matched_tracks:1,
              album_rating_key:"d",track_rating_keys:["4"],track_details:sampleDetails}]},
        ],
      };
    } else if (url.startsWith("/api/youtube-album?")) {
      body = albumFound
        ? {status:"found",artist:"Nils Frahm",album:"Felt (Deluxe Edition)",match_type:"edition",
           url:"https://music.youtube.com/playlist?list=OLAK5uy_verified"}
        : {status:"not_found"};
    } else if (url === "/api/playlist-covers/import-zip") {
      body = {
        count:3,new:1,updated:2,applied:2,unmatched:1,pending:0,failed:0,
        skipped:1,
        files:[
          {title:"3 AM",source:"3 AM.jpg",file:"3 AM.jpg",version:"updated",status:"applied",reason:""},
          {title:"Jazz",source:"Jazz.jpg",file:"Jazz.jpg",version:"updated",status:"applied",reason:""},
          {title:"Unknown",source:"Unknown.jpg",file:"Unknown.jpg",version:"new",status:"unmatched",reason:"No playlist"},
        ],
        skipped_files:[{file:"manifest.csv",reason:"Not a top-level JPG/PNG file"}]
      };
    } else if (url === "/api/inspect-config") {
      if (invalid) {
        status = 400;
        body = { detail: "Invalid playlist YAML" };
      } else {
        body = { new: alreadyLoaded ? [] : ["Beta"], updated: [], already_loaded: alreadyLoaded };
      }
    } else if (url === "/api/import-config") {
      imported = true;
      body = { comparison: { new: alreadyLoaded ? [] : ["Beta"], updated: [] } };
    } else if (url.startsWith("/api/sync-pending/")) {
      if (syncFails) {
        status = 500;
        body = { detail: "Plex connection lost" };
      } else {
        synced = true;
        body = { status: "synced", track_count: 1, matches: [] };
      }
    } else {
      throw Error("Unexpected URL: " + url);
    }
    return { ok: status < 400, status, json: async () => body };
  }
  vm.runInNewContext(script, { document, fetch });
  async function chooseFile() {
    // Let initial health/config fetches and DOM rendering settle.
    await new Promise((resolve) => setImmediate(resolve));
    const input = $("packFile");
    input.files = [{ name: filename, text: async () => "playlists:\n  Beta: {}\n" }];
    await input.onchange();
  }
  return { chooseFile, calls, requests, $, tabs, filename, get imported() { return imported; }, get synced() { return synced; } };
}

test("valid file immediately imports and syncs new playlists", async () => {
  const h = makeHarness();
  await h.chooseFile();
  const relevant = h.calls.filter((c) => ["/api/inspect-config", "/api/import-config"].includes(c) || c.startsWith("/api/sync-pending/"));
  assert.deepEqual(relevant, ["/api/inspect-config", "/api/import-config", "/api/sync-pending/Beta"]);
  assert.equal(h.imported, true);
  assert.equal(h.synced, true);
  assert.match(h.$("notice").textContent, /Imported "new-playlists.yml".*1 synced/);
  assert.equal(h.$("packFile").value, "");
});

test("invalid file never imports or syncs", async () => {
  const h = makeHarness({ invalid: true });
  await h.chooseFile();
  assert.equal(h.imported, false);
  assert.equal(h.synced, false);
  assert.ok(!h.calls.includes("/api/import-config"));
  assert.ok(!h.calls.some((url) => url.startsWith("/api/sync-pending/")));
  assert.match(h.$("notice").textContent, /Import rejected: Invalid playlist YAML/);
});

test("identical pack imports without needlessly syncing Plex", async () => {
  const h = makeHarness({ alreadyLoaded: true });
  await h.chooseFile();
  assert.equal(h.imported, true);
  assert.equal(h.synced, false);
  assert.ok(!h.calls.some((url) => url.startsWith("/api/sync-pending/")));
  assert.match(h.$("notice").textContent, /Nothing to sync/);
});

test("failed Plex sync keeps import and reports a retryable error", async () => {
  const h = makeHarness({ syncFails: true });
  await h.chooseFile();
  assert.equal(h.imported, true);
  assert.equal(h.synced, false);
  assert.ok(h.calls.includes("/api/sync-pending/Beta"));
  assert.match(h.$("notice").textContent, /1 errors/);
  assert.equal(h.$("syncPending").disabled, false);
});

test("compact album export remains available beside full library export", () => {
  assert.match(html, /href="\/api\/library-albums\.tsv"[^>]+download="plex-albums\.tsv"/);
  assert.match(html, /href="\/api\/library-export\.tsv"[^>]+download="plex-music-library\.tsv"/);
});

test("recent export form supports last hours and last N albums", async () => {
  const h = makeHarness();
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(html, /id="recentForm"[^>]+action="\/api\/library-recent.tsv"/);
  assert.match(html, /name="mode"/);
  assert.match(html, /name="limit"/);
  const mode = h.$("recentMode");
  const limit = h.$("recentLimit");
  assert.equal(typeof mode.onchange, "function");
  mode.value = "albums";
  mode.onchange();
  assert.equal(limit.value, "20");
  assert.equal(limit.max, "200");
  mode.value = "hours";
  mode.onchange();
  assert.equal(limit.value, "48");
  assert.equal(limit.max, "8760");
});

test("dashboard hides idle bulk controls and shows only pending work", async () => {
  const h = makeHarness();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(h.$("bulkPanel").hidden, true);
  await h.chooseFile();
  assert.equal(h.$("bulkPanel").hidden, true);
});


test("health and new-music tabs load analysis on demand, without changing playlists", async () => {
  const h = makeHarness();
  await new Promise(resolve => setImmediate(resolve));
  await h.tabs[1].onclick();
  assert.equal(h.$("viewHealth").hidden, false);
  assert.equal(h.$("viewPlaylists").hidden, true);
  assert.match(h.$("healthTotals").innerHTML, /not in playlists/);
  const count = h.calls.filter(c=>c.startsWith("/api/insights?")).length;
  await h.tabs.find(t=>t.dataset.view === "newmusic").onclick();
  assert.equal(h.$("viewNewmusic").hidden, false);
  assert.equal(h.calls.filter(c=>c.startsWith("/api/insights?")).length, count);
  assert.equal(h.calls.some(c=>c.startsWith("/api/curate-tracks")), false);
});

test("album lookup exposes the direct YouTube Music URL, not a search URL", async () => {
  const h = makeHarness();
  await new Promise(resolve => setImmediate(resolve));
  h.$("ytArtist").value = "Nils Frahm";
  h.$("ytAlbum").value = "Felt";
  await h.$("albumLookupForm").onsubmit({preventDefault(){}});
  const wrap = h.$("ytResult").children[0];
  assert.ok(wrap);
  const input = wrap.children.find(n=>n.value?.startsWith("https://music.youtube.com/"));
  assert.equal(input.value, "https://music.youtube.com/playlist?list=OLAK5uy_verified");
  const queueLink = wrap.children.find(n => n.textContent === "Review and queue album");
  assert.equal(queueLink.href, "/open-youtube-album?artist=Nils%20Frahm&album=Felt");
  assert.match(wrap.children[0].textContent, /Felt \(Deluxe Edition\).*edition match/);
  assert.doesNotMatch(input.value, /search\\?q=/);
});

test("album lookup never offers a generic search link when the album is absent", async () => {
  const h = makeHarness({albumFound:false});
  await new Promise(resolve => setImmediate(resolve));
  h.$("ytArtist").value = "Arve Henriksen";
  h.$("ytAlbum").value = "The Nature of Connections";
  await h.$("albumLookupForm").onsubmit({preventDefault(){}});
  assert.match(h.$("ytResult").textContent, /No album satisfies the deterministic matching rules/);
  assert.equal(h.$("ytResult").children.length, 0);
});


test("Duplicates view groups complete albums with candidate folders and edition warnings", async () => {
  const h = makeHarness();
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(!h.calls.some(c => c.startsWith("/api/duplicates?")));
  await h.tabs.find(t => t.dataset.view === "duplicates").onclick();
  assert.equal(h.$("viewDuplicates").hidden, false);
  assert.equal(h.$("viewHealth").hidden, true);
  assert.match(h.$("duplicateTotals").innerHTML, /complete duplicate albums/);
  assert.match(h.$("duplicateTotals").innerHTML, /folders to review/);
  assert.match(h.$("duplicateRows").innerHTML, /Waltz for Debby/);
  assert.match(h.$("duplicateRows").innerHTML, /\/music\/Bill Evans\/Original/);
  assert.match(h.$("duplicateRows").innerHTML, /You Must Believe in Spring/);
  assert.match(html, /href="\/api\/duplicates.tsv"/);
  h.$("duplicateKind").value = "edition";
  h.$("duplicateKind").onchange();
  assert.doesNotMatch(h.$("duplicateRows").innerHTML, /Waltz for Debby/);
  assert.match(h.$("duplicateRows").innerHTML, /You Must Believe in Spring/);
  h.$("duplicateSearch").value = "nothing";
  h.$("duplicateSearch").oninput();
  assert.match(h.$("duplicateRows").innerHTML, /No matching album duplicates/);
  h.$("duplicateSearch").value = "2022";
  h.$("duplicateSearch").oninput();
  assert.match(h.$("duplicateRows").innerHTML, /You Must Believe in Spring/);
  await h.$("refreshDuplicates").onclick();
  assert.ok(h.calls.includes("/api/duplicates?refresh=true"));
  assert.ok(!h.calls.some(c => /sync-pending|curate-tracks/.test(c)));
});


test("Choosing the retained album only marks other complete folders for manual review", async () => {
  const h = makeHarness();
  await new Promise(resolve => setImmediate(resolve));
  const node = h.$("duplicateRows");
  const pickers = [];
  node.querySelectorAll = (selector) => {
    if (selector !== ".duplicateKeep") return [];
    const picker = fakeElement();
    picker.dataset.group = "0";
    picker.value = "";
    pickers.push(picker);
    return [picker];
  };
  await h.tabs.find(t => t.dataset.view === "duplicates").onclick();
  assert.match(node.innerHTML, /Choose a copy to keep/);
  assert.doesNotMatch(node.innerHTML, /Review for removal/);
  const picker = pickers.at(-1);
  assert.equal(typeof picker.onchange, "function");
  picker.value = "1";
  picker.onchange();
  assert.match(node.innerHTML, /Keep selected/);
  assert.match(node.innerHTML, /Review for removal/);
  assert.match(node.innerHTML, /\/music\/Bill Evans\/Duplicate/);
  assert.ok(!h.calls.some(c=>/sync-pending|curate-tracks/.test(c)));
});


test("Discover tab compares curated albums with Plex and offers exact resolver URLs", async () => {
  const h = makeHarness();
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(!h.calls.some(x=>x.startsWith("/api/discover/isc?")));
  await h.tabs.find(x=>x.dataset.view==="isc").onclick();
  assert.equal(h.$("viewIsc").hidden, false);
  assert.match(h.$("iscTotals").innerHTML, /curated albums/);
  assert.match(h.$("iscRows").innerHTML, /Hiroshi Suzuki/);
  assert.match(h.$("iscRows").innerHTML, /Japanese jazz-funk/);
  assert.doesNotMatch(h.$("iscRows").innerHTML, /Paul Bley/);
  assert.match(h.$("iscRows").innerHTML,
    /\/open-youtube-album\?artist=Hiroshi%20Suzuki&amp;album=Cat/);
  assert.match(h.$("iscRows").innerHTML, /Find \/ download album/);
  assert.match(h.$("iscRows").innerHTML,
    /https:\/\/insheepsclothinghifi.com\/album\/hiroshi-suzuki-cat\//);
  h.$("iscStatus").value = "edition";
  h.$("iscStatus").onchange();
  assert.match(h.$("iscRows").innerHTML, /Hats/);
  assert.match(h.$("iscRows").innerHTML, /Hats \(Remastered\)/);
  h.$("iscStatus").value = "all";
  h.$("iscStatus").onchange();
  h.$("iscMood").value = "ecm-jazz";
  h.$("iscMood").onchange();
  assert.match(h.$("iscRows").innerHTML, /Paul Bley/);
  assert.doesNotMatch(h.$("iscRows").innerHTML, /Hiroshi Suzuki/);
  h.$("iscMood").value = "all";
  h.$("iscMood").onchange();
  h.$("iscSearch").value = "nowhere";
  h.$("iscSearch").oninput();
  assert.match(h.$("iscRows").innerHTML, /No albums match these filters/);
  await h.$("iscRefresh").onclick();
  assert.ok(h.calls.includes("/api/discover/isc?refresh=true"));
  assert.ok(!h.calls.some(c => /curate-tracks|sync-pending/.test(c)));
});


test("cover ZIP picker uploads raw ZIP, reports overwrites and unmatched, not YAML changes", async () => {
  const h = makeHarness();
  await new Promise(resolve => setImmediate(resolve));
  assert.match(html, /id="importCoverZip"[^>]*>Upload cover ZIP/);
  assert.match(html, /id="coverZipFile"[^>]+accept="\.zip/);
  const file = {name:"plex-playlist-covers-109.zip"};
  h.$("coverZipFile").files = [file];
  await h.$("coverZipFile").onchange();
  const request = h.requests.find(r=>r.url==="/api/playlist-covers/import-zip");
  assert.ok(request, "ZIP endpoint was called");
  assert.equal(request.options.method, "POST");
  assert.equal(request.options.headers["Content-Type"], "application/zip");
  assert.equal(request.options.body, file);
  assert.match(h.$("coverImportTotals").textContent, /3 saved.*1 new.*2 replaced.*2 applied/);
  assert.match(h.$("coverImportDetails").innerHTML, /Unknown.jpg/);
  assert.match(h.$("coverImportDetails").innerHTML, /manifest.csv/);
  assert.equal(h.$("coverImportResult").hidden, false);
  assert.equal(h.$("coverZipFile").value, "");
  assert.equal(h.$("importCoverZip").disabled, false);
  assert.ok(!h.calls.includes("/api/import-config"));
  assert.ok(!h.calls.some(r => r.startsWith("/api/sync-pending/")));
});

test("cover ZIP picker rejects nonzip files before sending requests", async () => {
  const h = makeHarness();
  await new Promise(resolve => setImmediate(resolve));
  h.$("coverZipFile").files = [{name:"image.png"}];
  await h.$("coverZipFile").onchange();
  assert.match(h.$("notice").textContent, /Select a ZIP file/);
  assert.ok(!h.calls.includes("/api/playlist-covers/import-zip"));
});
