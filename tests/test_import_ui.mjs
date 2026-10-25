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
    disabled: false, checked: false, dataset: {},
    addEventListener(name, callback) { this["on" + name] = callback; },
    setAttribute(name, value) { this[name] = value; },
    querySelectorAll() { return []; },
  };
}

function makeHarness({ invalid = false, alreadyLoaded = false, syncFails = false } = {}) {
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

  const document = {
    getElementById: $,
    querySelectorAll(query) {
      return query === ".build" ? rows.map((row) => row.querySelector(".build")) : [];
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
  return { chooseFile, calls, $, filename, get imported() { return imported; }, get synced() { return synced; } };
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
