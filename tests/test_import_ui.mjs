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

  const tabs = ["playlists", "health", "duplicates", "newmusic", "discover"].map(view => {
    const e = fakeElement();
    e.dataset.view = view;
    return e;
  });
  $("insightDays").value = "30";
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

  async function fetch(url) {
    calls.push(url);
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
    } else if (url.startsWith("/api/duplicates?")) {
      body = {
        summary: {
          library_tracks: 5, likely_groups: 1, likely_extra_entries: 1,
          edition_groups: 1, edition_entries_to_review: 2,
        },
        groups: [
          {kind:"likely",artist:"Bill Evans",title:"My Foolish Heart",
           albums:["Waltz for Debby"],entries:[
             {album:"Waltz for Debby",disc:1,track:1,duration_ms:299900,rating_key:"1"},
             {album:"Waltz for Debby",disc:1,track:1,duration_ms:300000,rating_key:"2"}]},
          {kind:"edition",artist:"Bill Evans",title:"B Minor Waltz",
           albums:["You Must Believe in Spring","You Must Believe in Spring (2022 Remaster)"],
           entries:[
             {album:"You Must Believe in Spring",disc:1,track:1,duration_ms:240000,rating_key:"3"},
             {album:"You Must Believe in Spring (2022 Remaster)",disc:1,track:1,duration_ms:240900,rating_key:"4"}]},
        ],
      };
    } else if (url.startsWith("/api/youtube-album?")) {
      body = albumFound
        ? {status:"found",artist:"Nils Frahm",album:"Felt (Deluxe Edition)",match_type:"edition",
           url:"https://music.youtube.com/playlist?list=OLAK5uy_verified"}
        : {status:"not_found"};
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
  return { chooseFile, calls, $, tabs, filename, get imported() { return imported; }, get synced() { return synced; } };
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
  assert.match(wrap.children[0].textContent, /Felt \(Deluxe Edition\).*edition match/);
  assert.doesNotMatch(input.value, /search\\?q=/);
});

test("album lookup never offers a generic search link when the album is absent", async () => {
  const h = makeHarness({albumFound:false});
  await new Promise(resolve => setImmediate(resolve));
  h.$("ytArtist").value = "Arve Henriksen";
  h.$("ytAlbum").value = "The Nature of Connections";
  await h.$("albumLookupForm").onsubmit({preventDefault(){}});
  assert.match(h.$("ytResult").textContent, /No matching album or recognized edition/);
  assert.equal(h.$("ytResult").children.length, 0);
});


test("Duplicates view scans on demand and filters read-only candidate groups", async () => {
  const h = makeHarness();
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(!h.calls.some(c => c.startsWith("/api/duplicates?")));
  await h.tabs.find(t => t.dataset.view === "duplicates").onclick();
  assert.equal(h.$("viewDuplicates").hidden, false);
  assert.equal(h.$("viewHealth").hidden, true);
  assert.match(h.$("duplicateTotals").innerHTML, /likely duplicate groups/);
  assert.match(h.$("duplicateRows").innerHTML, /My Foolish Heart/);
  assert.match(h.$("duplicateRows").innerHTML, /B Minor Waltz/);
  assert.match(html, /href="\/api\/duplicates.tsv"/);
  h.$("duplicateKind").value = "edition";
  h.$("duplicateKind").onchange();
  assert.doesNotMatch(h.$("duplicateRows").innerHTML, /My Foolish Heart/);
  assert.match(h.$("duplicateRows").innerHTML, /B Minor Waltz/);
  h.$("duplicateSearch").value = "nothing";
  h.$("duplicateSearch").oninput();
  assert.match(h.$("duplicateRows").innerHTML, /No matching duplicate candidates/);
  h.$("duplicateSearch").value = "2022";
  h.$("duplicateSearch").oninput();
  assert.match(h.$("duplicateRows").innerHTML, /B Minor Waltz/);
  await h.$("refreshDuplicates").onclick();
  assert.ok(h.calls.includes("/api/duplicates?refresh=true"));
  assert.ok(!h.calls.some(c => /sync-pending|curate-tracks/.test(c)));
});
