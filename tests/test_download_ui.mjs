// Fast UI regression: a failed job must not blank the entire download list.
// Run: node --test tests/test_download_ui.mjs
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const template = fs.readFileSync(
  new URL("../templates/index.html", import.meta.url), "utf8");

function node() {
  return {
    children: [], hidden: false, disabled: false, textContent: "",
    className: "", value: "", style: {}, dataset: {},
    classList: {add() {}, remove() {}},
    append(...children) {this.children.push(...children);},
    appendChild(child) {this.children.push(child); return child;},
    replaceChildren(...children) {this.children = children;},
    querySelectorAll() {return [];},
    setAttribute() {},
    addEventListener() {},
    closest() {return null;},
    scrollIntoView() {},
    get childElementCount() {return this.children.length;},
  };
}

function runUI(jobs) {
  const dom = new Map();
  const byId = id => {
    if (!dom.has(id)) dom.set(id, node());
    return dom.get(id);
  };
  const source = template.split("<script>")[1].split("</script>")[0]
    .replace("{{ jobs | tojson }}", JSON.stringify(jobs));
  const document = {
    getElementById: byId,
    createElement: node,
    createTextNode: value => ({textContent: value}),
    body: {contains() {return true;}},
    activeElement: null,
  };
  const context = {
    document, window: {location: {search: ""}, confirm() {return true;}},
    URLSearchParams, URL, fetch: async () => ({ok: false}),
    setInterval() {}, setTimeout() {}, clearTimeout() {},
    requestAnimationFrame() {},
    AbortController: class {},
  };
  vm.runInNewContext(source, context, {timeout: 1500});
  return {byId};
}

function job(id, status, source) {
  return {
    id, request_member_ids: [id], status, source,
    url: "https://music.youtube.com/playlist?list=OLAK5uy_example",
    source_title: id, message: "Test", playlist: true,
    attempt: 1, attempt_count: 1, total_items: 0, completed_items: 0,
    files: [], log: status === "failed" ? ["ERROR: HTTP 403 Forbidden"] : [],
    missing_tracks: [], progress: "",
    diagnosis: {heading: "Download failed", explanation: "HTTP 403",
                next_action: "Retry", tone: "error",
                duplicate_requests: 0, archived_items: 0},
  };
}

test("failed YouTube and Spotify jobs render instead of blanking the queue", () => {
  const jobs = [
    job("beethoven-403", "failed", "yt-dlp"),
    job("spotify-partial", "partial", "spotify"),
    job("completed", "succeeded", "yt-dlp"),
  ];
  const ui = runUI(jobs);
  assert.equal(ui.byId("jobs").children.length, 3);
  assert.match(ui.byId("job-overview").textContent, /2 failed\/partial/);
  assert.match(ui.byId("job-overview").textContent, /1 finished/);
});

test("failed job as first item still produces a visible error card", () => {
  const ui = runUI([job("beethoven-403", "failed", "yt-dlp")]);
  assert.equal(ui.byId("jobs").children.length, 1);
  assert.equal(ui.byId("jobs").children[0].dataset.jobId, "beethoven-403");
});
