// Fast, offline UI tests for Retry all failed (no server or YouTube required).
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const html = fs.readFileSync(new URL("../templates/index.html", import.meta.url), "utf8");
const source = html.split("<script>")[1].split("</script>")[0]
  .replace("{{ jobs | tojson }}", "[]");

function element() {
  return {
    children: [], hidden: false, disabled: false, textContent: "",
    value: "", className: "", dataset: {}, style: {}, listeners: {},
    classList: {add() {}, remove() {}},
    append(...parts) {this.children.push(...parts);},
    appendChild(item) {this.children.push(item); return item;},
    replaceChildren(...parts) {this.children = parts;},
    addEventListener(name, fn) {this.listeners[name] = fn;},
    setAttribute() {}, querySelectorAll() {return [];},
    querySelector() {return null;}, closest() {return null;},
    get childElementCount() {return this.children.length;},
  };
}

function harness({eligible=3, capacity=2, confirm=true}={}) {
  const nodes = new Map(), requests = [], prompts = [];
  const get = id => {
    if (!nodes.has(id)) nodes.set(id, element());
    return nodes.get(id);
  };
  get("history-filter").value = "all";
  const fetch = async (url, options={}) => {
    const path = String(url);
    requests.push({path, method:options.method || "GET"});
    if (path.includes("retry-failed/preview")) {
      return {ok:true, json:async()=>({
        ok:true, eligible, will_queue:Math.min(eligible,capacity),
        skipped_capacity:Math.max(0,eligible-capacity),
      })};
    }
    if (path === "/api/jobs/retry-failed") {
      return {ok:true, json:async()=>({
        ok:true, queued_count:Math.min(eligible,capacity),
        skipped_capacity:Math.max(0,eligible-capacity),
      })};
    }
    if (path === "/api/jobs") return {ok:true,json:async()=>[]};
    if (path.includes("/api/download-history?")) {
      return {ok:true,json:async()=>({
        total:0, all_attempts:0, all_status_counts:{}, items:[],
      })};
    }
    throw Error("Unexpected request " + path);
  };
  const document = {
    getElementById: get, createElement: element,
    createTextNode: text => ({textContent:text}),
    activeElement:null, body:{contains() {return true;}},
  };
  vm.runInNewContext(source, {
    document,
    window:{location:{search:""},confirm(message){prompts.push(message);return confirm;}},
    fetch, URLSearchParams, Date, setInterval() {}, setTimeout() {},
    clearTimeout() {}, requestAnimationFrame() {},
    AbortController:class {},
  }, {timeout:3000});
  return {get, requests, prompts};
}

async function settle() {
  for (let i=0; i<10; i++) await new Promise(resolve=>setImmediate(resolve));
}

test("confirmed click previews before POST, displays queued/skipped counts", async () => {
  const h = harness({eligible:3,capacity:2});
  await settle();
  await h.get("retry-all-failed").listeners.click();
  await settle();
  const preview = h.requests.findIndex(x=>x.path.includes("retry-failed/preview"));
  const submit = h.requests.findIndex(x=>x.path==="/api/jobs/retry-failed");
  assert.ok(preview >= 0 && submit > preview);
  assert.equal(h.prompts.length,1);
  assert.match(h.prompts[0],/Queue 2 distinct failed album/);
  assert.match(h.get("job-notice").textContent,/2 queued, 1 not queued/);
  assert.equal(h.get("retry-all-failed").disabled,false);
});

test("cancelled confirmation never submits downloads", async () => {
  const h = harness({confirm:false});
  await settle();
  await h.get("retry-all-failed").listeners.click();
  assert.equal(h.prompts.length,1);
  assert.equal(h.requests.filter(x=>x.path==="/api/jobs/retry-failed").length,0);
});

test("full queue is reported without offering a misleading confirmation", async () => {
  const h = harness({eligible:4,capacity:0});
  await settle();
  await h.get("retry-all-failed").listeners.click();
  assert.equal(h.prompts.length,0);
  assert.match(h.get("history-status").textContent,/Queue is full/);
  assert.equal(h.requests.filter(x=>x.path==="/api/jobs/retry-failed").length,0);
});
