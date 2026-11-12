import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import vm from "node:vm";

const root = new URL("../app/", import.meta.url);
const script = fs.readFileSync(new URL("static/signal-console.js", root), "utf8");
const css = fs.readFileSync(new URL("static/site-navigation.css", root), "utf8");
const header = fs.readFileSync(new URL("templates/_site_navigation.html", root), "utf8");
new vm.Script(script, { filename:"signal-console.js" });

function page(initial=null, opts={}) {
  const storage = new Map();
  if (initial!==null) storage.set("signal-console-theme-v1",initial);
  const handlers={};
  const select={
    value:"cyber",
    addEventListener(event,callback){handlers[event]=callback;},
  };
  const mark={src:"http://media.local/static/signal-mark.svg"};
  const root={dataset:{}};
  let writes=0;
  const localStorage={
    getItem(key){if(opts.blockStorage) throw Error("blocked");return storage.get(key)||null;},
    setItem(key,val){if(opts.blockStorage) throw Error("blocked");storage.set(key,val);writes+=1;},
  };
  const document={
    documentElement:root,
    getElementById:id=>id==="signal-theme-toggle"?select:null,
    querySelector:query=>query===".signal-mark"?mark:null,
  };
  vm.runInNewContext(script,{document,localStorage,Set});
  return {
    root,select,mark,storage,
    get writes(){return writes;},
    change(value){select.value=value;handlers.change()},
  };
}

test("single accessible theme selector exposes Cyber, Slate and Terminal",()=>{
  assert.equal((header.match(/id="signal-theme-toggle"/g)||[]).length,1);
  assert.match(header,/<label for="signal-theme-toggle"/);
  assert.match(header,/<select id="signal-theme-toggle"/);
  assert.doesNotMatch(header,/aria-pressed=/);
  for(const name of ["cyber","slate","terminal"])
    assert(header.includes('<option value="'+name+'">'));
  assert.equal((header.match(/class="workspace-topbar"/g)||[]).length,1);
});

test("theme defaults to Cyber, changes without reload and persists",()=>{
  const p=page();
  assert.equal(p.root.dataset.signalTheme,"cyber");
  assert.equal(p.select.value,"cyber");
  assert.equal(p.mark.src,"http://media.local/static/signal-mark.svg");
  p.change("terminal");
  assert.equal(p.root.dataset.signalTheme,"terminal");
  assert.equal(p.select.value,"terminal");
  assert.equal(p.mark.src,"http://media.local/static/signal-mark-static.svg");
  assert.equal(p.storage.get("signal-console-theme-v1"),"terminal");
  p.change("slate");
  assert.equal(p.root.dataset.signalTheme,"slate");
  p.change("cyber");
  assert.equal(p.mark.src,"http://media.local/static/signal-mark.svg");
  assert.equal(p.storage.get("signal-console-theme-v1"),"cyber");
});

test("persisted terminal preference is respected on another workspace page",()=>{
  const p=page("terminal");
  assert.equal(p.root.dataset.signalTheme,"terminal");
  assert.equal(p.select.value,"terminal");
  assert.equal(p.mark.src,"http://media.local/static/signal-mark-static.svg");
  assert.equal(p.storage.get("signal-console-theme-v1"),"terminal");
});

test("invalid saved preference falls back safely to original theme",()=>{
  const p=page("nonexistent");
  assert.equal(p.root.dataset.signalTheme,"cyber");
  assert.equal(p.select.value,"cyber");
  assert.equal(p.storage.get("signal-console-theme-v1"),"cyber");
  p.change("garbage");
  assert.equal(p.root.dataset.signalTheme,"cyber");
});

test("private browsing still permits in-page theme changes",()=>{
  const p=page(null,{blockStorage:true});
  p.change("terminal");
  assert.equal(p.root.dataset.signalTheme,"terminal");
  assert.equal(p.select.value,"terminal");
});

test("terminal design only applies to terminal and respects motion preferences",()=>{
  assert(css.includes('html[data-signal-theme="terminal"]'));
  assert(css.includes('html[data-signal-theme="terminal"] body'));
  assert(css.includes('html[data-signal-theme="terminal"] .home-task'));
  assert(css.includes('html[data-signal-theme="terminal"] .workspace-topbar'));
  assert(css.includes('html[data-signal-theme="terminal"] button.primary'));
  assert(css.includes('@media(prefers-reduced-motion:reduce)'));
  assert(css.includes("color-scheme:dark"));
});
