// Fast standalone browser-flow checks for the album progress page.
// Run: node --test tests/test_album_download_ui.mjs
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const template = fs.readFileSync(
  new URL("../app/templates/album_download.html", import.meta.url), "utf8");
const source = template.split("<script>", 2)[1]?.split("</script>", 1)[0]
  .replace("{{ artist | tojson }}", JSON.stringify("Jim Hall"))
  .replace("{{ album | tojson }}", JSON.stringify("Jim Hall Live!"));
assert.ok(source && source.includes("fetchJson"), "Album download script must be present");

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
function fakeElement() {
  return {
    textContent: "", hidden: false, disabled: false, href: "", action: "",
    attributes: {}, children: [], handlers: {},
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(name, callback) { this.handlers[name] = callback; },
    querySelector() { return this.spinner || (this.spinner = fakeElement()); },
    append(child) { this.children.push(child); },
    replaceChildren(...children) { this.children = children; },
  };
}
function harness(fetchHandler) {
  const elements = new Map();
  const calls = [];
  const navigation = [];
  const timers = [];
  function byId(id) {
    if (!elements.has(id)) elements.set(id, fakeElement());
    return elements.get(id);
  }
  byId("queue-album-form").action = "http://media-server:5051/queue-youtube-album";
  const formData = [["artist", "Jim Hall"], ["album", "Jim Hall Live!"], ["token", "valid-token"]];
  class MockFormData {
    constructor(form) { assert.equal(form, byId("queue-album-form")); }
    *[Symbol.iterator]() { yield* formData; }
  }
  const context = {
    document: {
      getElementById: byId,
      createElement: fakeElement,
    },
    fetch: (url, options) => {
      calls.push({url: String(url), options});
      return fetchHandler(String(url), options);
    },
    URLSearchParams,
    FormData: MockFormData,
    window: {location: {assign: url => navigation.push(url)}},
    performance: {now: () => 0},
    setInterval: () => 1,
    setTimeout: fn => { timers.push(fn); return 1; },
  };
  vm.runInNewContext(source, context, {timeout: 1000});
  return {byId, calls, navigation, timers};
}
function answer(body, status=200) {
  return {ok: status < 400, status, json: async () => body};
}
async function settle() {
  for (let i=0; i<6; i++) await new Promise(resolve => setImmediate(resolve));
}
const found = {
  status: "found", artist: "Jim Hall", album: "Live!",
  url: "https://music.youtube.com/browse/MPREb_IObRABnIPO3",
  match_type: "artist_prefix", artist_match_type: "exact",
};

test("initial page displays searching status before slow catalog responds", async () => {
  const slow = deferred();
  const h = harness(() => slow.promise);
  assert.equal(h.byId("status").textContent, "Searching YouTube Music…");
  assert.equal(h.byId("elapsed").textContent, "Elapsed: 0 s");
  assert.equal(h.byId("activity").attributes["aria-busy"], "true");
  assert.equal(h.calls.length, 1);
  assert.match(h.calls[0].url, /\/api\/youtube-album\?/);
  slow.resolve(answer({status: "not_found"}));
  await settle();
  assert.equal(h.navigation.length, 1);
  assert.match(h.navigation[0], /music\.youtube\.com\/search\?/);
  assert.equal(h.calls.length, 1);
});

test("exact match transitions to download status before slow POST acknowledgement", async () => {
  const slowPost = deferred();
  const h = harness((url, options) => (
    options?.method === "POST" ? slowPost.promise : Promise.resolve(answer(found))
  ));
  await settle();
  assert.equal(h.byId("status").textContent, "Checking Plex and contacting yt-dlp…");
  assert.equal(h.byId("heading").textContent, "Submitting album…");
  assert.equal(h.byId("queue-button").disabled, true);
  assert.equal(h.calls.length, 2);
  assert.equal(h.calls[1].options.method, "POST");
  assert.equal(h.calls[1].options.headers.Accept, "application/json");
  assert.equal(h.navigation.length, 0);
  slowPost.resolve(answer({
    ok:true, job_id: "job123", status:"queued", duplicate:false,
    downloader_url:"http://media-server:4545/",
  }));
  await settle();
  assert.equal(h.byId("heading").textContent, "Added to the download queue");
  assert.match(h.byId("status").textContent, /Job job123 · queued/);
  assert.equal(h.navigation.length, 0);
  h.timers[0]();
  assert.deepEqual(h.navigation, ["http://media-server:4545/"]);
});

test("existing download has explicit acknowledgement without a second submission", async () => {
  const h = harness((url, options) => Promise.resolve(answer(
    options?.method === "POST"
      ? {ok:true, duplicate:true, job_id:"oldJob", status:"succeeded", downloader_url:"http://media-server:4545/"}
      : found
  )));
  await settle();
  assert.equal(h.byId("heading").textContent, "Already in the download queue");
  assert.equal(h.calls.length, 2);
  assert.match(h.byId("status").textContent, /oldJob · succeeded/);
});

test("edition match waits for explicit manual button and reports errors", async () => {
  const edition = {...found, match_type:"edition", album:"Live! (Remastered)"};
  const h = harness((url, options) => Promise.resolve(
    options?.method === "POST"
      ? answer({detail: "This exact album is already in Plex."}, 409)
      : answer(edition)
  ));
  await settle();
  assert.equal(h.byId("status").textContent, "Review required before download.");
  assert.equal(h.byId("queue-button").hidden, false);
  assert.equal(h.calls.length, 1);
  const event = {preventDefault() {this.prevented = true;}};
  await h.byId("queue-album-form").handlers.submit(event);
  assert.equal(event.prevented, true);
  assert.equal(h.calls.length, 2);
  assert.equal(h.byId("heading").textContent, "Download not started");
  assert.equal(h.byId("status").textContent, "This exact album is already in Plex.");
  assert.equal(h.byId("retry-button").hidden, false);
  assert.equal(h.navigation.length, 0);
});

test("ambiguous catalog matches never submit or download", async () => {
  const h = harness(() => Promise.resolve(answer({
    status:"ambiguous",
    candidates:[{artist:"Jim Hall",album:"Live!",url:"https://music.youtube.com/browse/MPREone"}],
  })));
  await settle();
  assert.equal(h.byId("heading").textContent, "Several albums match");
  assert.equal(h.byId("candidates").hidden, false);
  assert.equal(h.byId("candidates").children.length, 1);
  assert.equal(h.calls.length, 1);
  assert.equal(h.navigation.length, 0);
});
