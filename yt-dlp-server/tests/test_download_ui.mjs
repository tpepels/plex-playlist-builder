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

function runUI(jobs, search = "") {
  const dom = new Map();
  const historyChanges = [];
  let nextJobs = jobs;
  let jobPoll;
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
    document, window: {
      location: {search, pathname: "/"}, confirm() {return true;},
      history: {replaceState(_state, _title, path) {historyChanges.push(path);}},
    },
    URLSearchParams, URL, fetch: async (path) => path === "/api/jobs"
      ? {ok:true,json:async()=>nextJobs} : {ok:false},
    setInterval(callback,ms) {if(ms===5000)jobPoll=callback;},
    setTimeout() {}, clearTimeout() {},
    requestAnimationFrame() {},
    AbortController: class {},
  };
  vm.runInNewContext(source, context, {timeout: 1500});
  return {byId, historyChanges,
    setJobs(next){nextJobs=next},pollJobs(){return jobPoll?.()}};
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

test("active and metadata jobs appear once as detailed cards, finished jobs do not", () => {
  const jobs = [
    job("beethoven-403", "failed", "yt-dlp"),
    job("spotify-partial", "partial", "spotify"),
    job("completed", "succeeded", "yt-dlp"),
    job("queued", "queued", "yt-dlp"),
    job("running", "running", "yt-dlp"),
    job("metadata", "needs_metadata", "yt-dlp"),
  ];
  const ui = runUI(jobs);
  assert.equal(ui.byId("jobs").children.length, 3);
  assert.deepEqual(
    ui.byId("jobs").children.map(card => card.dataset.jobId),
    ["queued", "running", "metadata"]
  );
  assert.match(ui.byId("job-overview").textContent, /2 failed\/partial/);
  assert.match(ui.byId("job-overview").textContent, /1 finished/);
});

test("terminal jobs do not create a second detailed card", () => {
  const ui = runUI([job("beethoven-403", "failed", "yt-dlp")]);
  assert.equal(ui.byId("jobs").children.length, 1);
  assert.match(ui.byId("jobs").children[0].textContent, /No active downloads/);
});

test("unsupported stale job IDs leave no notice and are removed from the address bar", () => {
  const ui = runUI([], "?job=stale-123&result=duplicate");
  assert.equal(ui.byId("job-notice").hidden, true);
  assert.deepEqual(ui.historyChanges, ["/"]);
});

test("deep-linked job notice names its album rather than an opaque job ID", () => {
  const active = {...job("internal-id", "running", "yt-dlp"),
    source_title: "Live at the Vanguard", source_album_artist: "Jazz Artist"};
  const ui = runUI([active], "?job=internal-id&result=duplicate");
  assert.equal(ui.byId("job-notice").hidden, false);
  assert.match(ui.byId("job-notice").textContent, /Jazz Artist — Live at the Vanguard/);
  assert.doesNotMatch(ui.byId("job-notice").textContent, /internal-id/);
  assert.deepEqual(ui.historyChanges, ["/"]);
});

test("static server help and tool versions are collapsed by default", () => {
  assert.match(template, /<details class="compact-help">\s*<summary>Supported sources/);
  assert.match(template, /<details class="compact-help version-details">/);
  assert.match(template, /<summary>Advanced options<\/summary>/);
});

function urlsFromTree(node, result=[]) {
  if (node.href) result.push(node.href);
  for (const child of node.children || []) urlsFromTree(child,result);
  return result;
}

test("a queue poll with unchanged data does not rebuild the download page",async()=>{
  const running=job("unchanged-job", "running", "yt-dlp");
  const ui=runUI([running]);
  const old=ui.byId("jobs").children[0];
  await ui.pollJobs();
  assert.equal(ui.byId("jobs").children[0],old,
    "Unchanged network polling must preserve expanded cards, focus and scroll");
  ui.setJobs([{...running,progress:"50%"}]);
  await ui.pollJobs();
  assert.notEqual(ui.byId("jobs").children[0],old);
});

test("every visible download has a direct Plex-import diagnostic link",()=>{
  const ui=runUI([job("queued-job-id","queued","yt-dlp")]);
  const urls=urlsFromTree(ui.byId("jobs"));
  assert(urls.includes("/downloads-audit?tab=history&q=queued-job-id"));
});
