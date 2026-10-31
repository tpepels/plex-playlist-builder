// Fast UI regressions: show every failed album without a search or disclosure.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const template = fs.readFileSync(new URL("../templates/index.html", import.meta.url), "utf8");
const script = template.split("<script>")[1].split("</script>")[0]
  .replace("{{ jobs | tojson }}", "[]");

function element() {
  return {
    textContent: "", className: "", value: "", hidden: false, disabled: false,
    children: [], dataset: {}, style: {}, listeners: {},
    classList: {add() {}, remove() {}},
    append(...nodes) {this.children.push(...nodes);},
    appendChild(node) {this.children.push(node); return node;},
    replaceChildren(...nodes) {this.children = nodes;},
    addEventListener(event, callback) {this.listeners[event] = callback;},
    setAttribute() {}, querySelectorAll() {return [];}, querySelector() {return null;},
    get childElementCount() {return this.children.length;},
  };
}

function attempt(i, status) {
  return {
    job_id: "job-" + i, artist: "Performer " + i, title: "Album " + i,
    status, source: "yt-dlp", title_known: true,
    reason: status === "failed" ? "ERROR: unable to download video data: HTTP Error 403" : "Published",
    created_at: 1000 + i, completed_items: 0, total_items: 10,
    url: "https://music.youtube.com/playlist?list=OLAK5uy_test" + i,
    has_retried: false, removed_from_queue: false,
    final_dir: status === "succeeded" ? "/music" : "",
  };
}
function runUI(attempts) {
  const nodes = new Map(), calls = [];
  const get = id => {
    if (!nodes.has(id)) nodes.set(id, element());
    return nodes.get(id);
  };
  get("history-filter").value = "all";
  get("history-query").value = "";
  const document = {
    getElementById: get, createElement: element,
    createTextNode: text => ({textContent: text}),
    activeElement: null, body: {contains() {return true;}},
  };
  const fetch = async (url, options) => {
    calls.push(String(url));
    const params = new URL(String(url), "http://localhost").searchParams;
    const offset = Number(params.get("offset") || "0");
    const limit = Number(params.get("limit") || "200");
    assert.equal(params.get("status"), "all", "default history must not hide failed attempts");
    return {ok: true, json: async () => ({
      total: attempts.length, all_attempts: attempts.length,
      all_status_counts: {
        failed: attempts.filter(x => x.status === "failed").length,
        succeeded: attempts.filter(x => x.status === "succeeded").length,
      },
      items: attempts.slice(offset, offset + limit),
    })};
  };
  vm.runInNewContext(script, {
    document, window: {location: {search: ""}, confirm() {return true;}},
    fetch, URLSearchParams, Date,
    setInterval() {}, setTimeout() {}, requestAnimationFrame() {},
    AbortController: class {},
  }, {timeout: 2500});
  return {get, calls};
}
async function flush() {
  for (let i=0; i<15; i++) await new Promise(resolve => setImmediate(resolve));
}
function rowText(node) {
  return [node.textContent, ...node.children.flatMap(rowText)].join(" ");
}

test("39 failed album titles are visible without searching", async () => {
  const attempts = [
    ...Array.from({length: 39}, (_,i) => attempt(i, "failed")),
    ...Array.from({length: 11}, (_,i) => attempt(39+i, "succeeded")),
  ];
  const ui = runUI(attempts);
  await flush();
  assert.equal(ui.get("history-results").children.length, 52);
  assert.match(ui.get("history-summary").textContent, /50 saved attempts · 39 failed\/partial/);
  assert.match(ui.get("history-results").children[0].textContent, /Failed and partial downloads/);
  assert.match(rowText(ui.get("history-results").children[1]), /Album 38/);
  assert.equal(ui.calls.length, 1);
  assert.ok(!template.includes('id="history-details"'));
});

test("optional filters update visible rows without a new request", async () => {
  const ui = runUI([attempt(1, "failed"), attempt(2, "succeeded")]);
  await flush();
  assert.equal(ui.get("history-results").children.length, 4);
  ui.get("history-filter").value = "failed";
  ui.get("history-filter").listeners.change();
  assert.equal(ui.get("history-results").children.length, 2);
  ui.get("history-filter").value = "all";
  ui.get("history-filter").listeners.change();
  assert.equal(ui.get("history-results").children.length, 4);
  assert.equal(ui.calls.length, 1);
});

test("all pages load automatically for Ctrl+F across historical jobs", async () => {
  const attempts = Array.from({length: 215}, (_,i) => attempt(i, i%4 ? "failed" : "succeeded"));
  const ui = runUI(attempts);
  await flush();
  assert.equal(ui.calls.length, 2);
  assert.equal(ui.get("history-results").children.length, 217);
  assert.match(ui.get("history-status").textContent, /215 of 215 loaded attempts visible/);
});
