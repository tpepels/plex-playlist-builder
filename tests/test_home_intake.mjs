import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const js = fs.readFileSync(new URL("../app/static/home-intake.js", import.meta.url), "utf8");
const html = fs.readFileSync(new URL("../app/templates/home.html", import.meta.url), "utf8");
new vm.Script(js, {filename: "home-intake.js"});

function harness({inspect, importResult, syncResponse}={}) {
  const ids=[
    "home-playlist-file","home-playlist-drop","home-file-label",
    "home-import-status","home-import-preview","home-import-summary",
    "home-import-description","home-import-confirm","home-import-cancel",
    "home-import-progress-panel","home-import-progress","home-import-current",
    "home-import-errors","refresh-home",
  ];
  const elements = new Map();
  function el(id) {
    const classes = new Set();
    const node = {
      id,value:"",textContent:"",className:"",hidden:false,disabled:false,
      listeners:{},children:[],files:[],
      classList:{add:s=>classes.add(s),remove:s=>classes.delete(s),
        contains:s=>classes.has(s)},
      addEventListener(name,handler){this.listeners[name]=handler},
      replaceChildren(...children){this.children=children},
      click(){if(this.listeners.click)return this.listeners.click()},
    };
    elements.set(id,node);return node;
  }
  for(const id of ids)el(id);
  const calls = [];
  let refreshes=0;
  elements.get("refresh-home").click=()=>{refreshes++};
  const fakeInspect=inspect ?? {new:["New"],updated:["Changed"],
    unchanged:["Same"],retained:["Local"],already_loaded:false};
  const fakeImport=importResult ?? {changed:["New","Changed"],version:{id:"v42"}};
  const fetch=async (url,options)=>{
    const body=JSON.parse(options.body||"{}");
    calls.push({url,body});
    let result, status=200;
    if(url==="/api/inspect-config") {
      if(inspect instanceof Error){result={detail:inspect.message};status=400}
      else result=fakeInspect;
    } else if(url==="/api/import-config") result=fakeImport;
    else if(url.startsWith("/api/sync-pending/")) {
      const name=decodeURIComponent(url.split("/").at(-1));
      result=syncResponse?.(name)??{status:"synced"};
      if(result instanceof Error){status=503;result={detail:result.message}}
    } else throw Error("Unexpected call "+url);
    return {ok:status<400,status,json:async()=>result};
  };
  const document={getElementById:id=>elements.get(id)||null,
    createElement:tag=>({tag,textContent:""})};
  vm.runInNewContext(js,{document,fetch,AbortController,setTimeout,clearTimeout});
  const file={name:"playlists.yml",size:160,
    text:async()=> "playlists:\n  New:\n    items:\n      - rating_key: 1\n"};
  async function select(f=file){
    const input=elements.get("home-playlist-file");
    input.files=[f];
    await input.listeners.change();
  }
  return {elements,calls,select,file,get refreshes(){return refreshes},
    get:id=>elements.get(id)};
}

test("Home serves the two complete intake paths without extra navigation",()=>{
  assert.match(html, /include "_site_navigation.html"/);
  assert.match(html, /action="\/downloads\/engine\/download" method="post"/);
  assert.match(html, /id="home-download-url" type="url" name="url" required/);
  assert.match(html, /id="home-download-playlist" type="checkbox" name="playlist" checked/);
  assert.match(html, /id="home-playlist-file" type="file"/);
  assert.match(html, /id="home-import-confirm"/);
  assert.match(html, /src="\/static\/home-intake.js"/);
  assert.doesNotMatch(html, /home-top-nav|class="home-hero"/);
});

test("YAML is inspected before any changes and syncs only changed entries",async()=>{
  const h=harness();
  await h.select();
  assert.deepEqual(h.calls.map(c=>c.url),["/api/inspect-config"]);
  assert.equal(h.get("home-import-preview").hidden,false);
  assert.equal(h.get("home-import-confirm").disabled,false);
  assert.match(h.get("home-import-description").textContent,/retained/);
  assert.equal(h.get("home-import-summary").children.length,4);
  await h.get("home-import-confirm").listeners.click();
  assert.deepEqual(h.calls.map(c=>c.url),[
    "/api/inspect-config", "/api/import-config",
    "/api/sync-pending/New", "/api/sync-pending/Changed",
  ]);
  assert.equal(h.calls[1].body.filename,"playlists.yml");
  assert.equal(h.calls[2].body.version_id,"v42");
  assert.equal(h.get("home-import-progress").value,2);
  assert.match(h.get("home-import-current").textContent,/2 synced/);
  assert.equal(h.refreshes,1);
});

test("Already-loaded YAML has no import or sync action",async()=>{
  const h=harness({inspect:{new:[],updated:[],unchanged:["A"],retained:[],already_loaded:true}});
  await h.select();
  assert.equal(h.get("home-import-confirm").disabled,true);
  assert.match(h.get("home-import-status").textContent,/No new or modified/);
  assert.equal(h.calls.length,1);
});

test("Invalid extension and oversize file never reach server",async()=>{
  const h=harness();
  await h.select({name:"track.mp3",size:50,text:async()=>""});
  assert.equal(h.calls.length,0);
  assert.match(h.get("home-import-status").textContent,/Choose a .yml or .yaml/);
  await h.select({name:"bad.yml",size:2_097_153,text:async()=>""});
  assert.equal(h.calls.length,0);
  assert.match(h.get("home-import-status").textContent,/2 MB/);
});

test("Invalid YAML cannot be imported",async()=>{
  const h=harness({inspect:new Error("Invalid playlist YAML")});
  await h.select();
  assert.deepEqual(h.calls.map(c=>c.url),["/api/inspect-config"]);
  assert.equal(h.get("home-import-preview").hidden,true);
  assert.match(h.get("home-import-status").textContent,/Invalid playlist YAML/);
});

test("Plex syncing failure leaves saved import and reports retry path",async()=>{
  const h=harness({syncResponse:name=>name==="Changed"
    ? new Error("Plex offline") : {status:"synced"}});
  await h.select();
  await h.get("home-import-confirm").listeners.click();
  assert.equal(h.get("home-import-progress").value,2);
  assert.match(h.get("home-import-errors").textContent,/Changed: Plex offline/);
  assert.match(h.get("home-import-status").textContent,/definitions were saved/);
  assert.equal(h.get("home-import-status").className.includes("error"),true);
});

test("Cancel clears preview without importing",async()=>{
  const h=harness();
  await h.select();
  h.get("home-import-cancel").listeners.click();
  assert.equal(h.get("home-import-preview").hidden,true);
  assert.equal(h.get("home-import-confirm").disabled,true);
  assert.equal(h.calls.length,1);
});
