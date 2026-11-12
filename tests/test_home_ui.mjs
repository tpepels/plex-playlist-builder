import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const source=fs.readFileSync(new URL("../app/static/home.js",import.meta.url),"utf8");
new vm.Script(source,{filename:"home.js"});

function makeNode(id="") {
  const classes=new Set();
  return {
    id,hidden:false,disabled:false,textContent:"",children:[],listeners:{},attrs:{},
    classList:{toggle(key,enabled){if(enabled)classes.add(key);else classes.delete(key)},
      contains(key){return classes.has(key)}},
    append(...nodes){this.children.push(...nodes)},
    replaceChildren(...nodes){this.children=nodes},
    setAttribute(key,value){this.attrs[key]=value},
    addEventListener(key,callback){this.listeners[key]=callback},
  };
}

function flatten(node){
  return [node.textContent||"",...node.children.flatMap(flatten)].join(" ");
}

async function makeHarness(sources){
  const ids=[
    "overview-updated","plex-indicator","plex-value","plex-detail",
    "playlist-count","playlist-detail","sync-count","sync-detail",
    "download-indicator","download-count","download-detail","maintenance-status","attention-list",
    "unindexed-count","unindexed-detail","drift-count","drift-detail"
  ];
  const elements=Object.fromEntries(ids.map(id=>[id,makeNode(id)]));
  const calls=[];
  const document={
    hidden:false,
    getElementById(id){return elements[id]||null},
    createElement(tag){return makeNode(tag)},
  };
  const fetch=async (path, options)=>{
    assert.equal(options.cache,"no-store");
    assert.equal(options.headers.Accept,"application/json");
    calls.push(path);
    const value=sources[path];
    if(value instanceof Error)throw value;
    if(!value)return {ok:false,status:503,json:async()=>({})};
    return {ok:true,status:200,json:async()=>value};
  };
  let interval;let changeHandler;
  vm.runInNewContext(source,{
    document,fetch,AbortController,setTimeout,clearTimeout,
    window:{addEventListener(type,callback){if(type==="workspace:changed")changeHandler=callback},
      setInterval(callback,intervalMs){
      assert.equal(intervalMs,30000);interval=callback;
    }}
  });
  await new Promise(resolve=>setTimeout(resolve,20));
  return {elements,calls,document,sources,interval,changeHandler};
}

test("home renders live Plex, playlist, downloader and attention counters",async()=>{
  const h=await makeHarness({
    "/api/health":{ok:true,server:"Plex Home",library:"Music"},
    "/api/config/summary":{playlists:["Alpha","Beta","Gamma","Delta"],
      version:{counts:{total:4,needs_sync:2}}},
    "/api/downloads/bridge/status":{ok:true,counts:{
      queued:1,running:2,finalizing:0,failed:1,partial:0,needs_metadata:1
    },plex_sync:{pending:1}},
    "/api/workspace/maintenance":{enabled:true,jobs:{
      audio:{last_success:"2026-11-11T18:00:00Z",pending:40},
      library:{last_success:"2026-11-11T18:00:00Z",pending:0}}}
  });
  const e=h.elements;
  assert.equal(e["plex-value"].textContent,"Connected");
  assert.equal(e["plex-detail"].textContent,"Plex Home · Music");
  assert.equal(e["playlist-count"].textContent,"4");
  assert.equal(e["sync-count"].textContent,"2");
  assert.equal(e["download-count"].textContent,"3");
  assert.equal(e["plex-indicator"].textContent,"Online");
  assert.equal(e["download-indicator"].textContent,"Online");
  assert.equal(e["attention-list"].children.length,3);
  const message=flatten(e["attention-list"]);
  assert.match(message,/2 playlists awaiting sync/);
  assert.match(message,/2 downloads need attention/);
  assert.match(message,/1 release\(s\) awaiting Plex scan/);

  assert.equal(h.calls.length,4);
  assert.match(e["maintenance-status"].textContent,/40 cache records queued/);
  assert.equal(e["attention-list"].children[0].children[2]?.href,"/playlists?pending=1");
});

test("partial outages report explicit problems and do not invent success",async()=>{
  const h=await makeHarness({
    "/api/health":new Error("Plex offline"),
    "/api/config/summary":{playlists:[],version:{counts:{total:0,needs_sync:0}}},
    "/api/downloads/bridge/status":new Error("yt-dlp offline"),
    "/api/workspace/maintenance":{enabled:true,jobs:{audio:{},library:{}}}
  });
  assert.equal(h.elements["plex-value"].textContent,"Offline");
  assert.equal(h.elements["download-count"].textContent,"—");
  assert.equal(h.elements["plex-indicator"].classList.contains("offline"),true);
  assert.equal(h.elements["download-indicator"].classList.contains("offline"),true);
  assert.match(flatten(h.elements["attention-list"]),/Plex is unreachable/);
  assert.match(flatten(h.elements["attention-list"]),/Download engine is offline/);
  assert.match(h.elements["overview-updated"].textContent,/Partial status/);
});

test("workspace change notification recovers from failed services without mutating playlists",async()=>{
  const statuses={
    "/api/health":new Error("offline"),
    "/api/config/summary":new Error("storage offline"),
    "/api/downloads/bridge/status":new Error("offline"),
    "/api/workspace/maintenance":new Error("offline")
  };
  const h=await makeHarness(statuses);
  statuses["/api/health"]={ok:true,server:"Plex",library:"Music"};
  statuses["/api/config/summary"]={playlists:[],version:{counts:{total:0,needs_sync:0}}};
  statuses["/api/downloads/bridge/status"]={ok:true,counts:{}};
  statuses["/api/workspace/maintenance"]={enabled:true,jobs:{audio:{pending:0},library:{}}};
  h.changeHandler();
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(h.elements["plex-value"].textContent,"Connected");
  assert.equal(h.elements["playlist-count"].textContent,"0");
  assert.equal(h.elements["download-count"].textContent,"0");
  assert.match(flatten(h.elements["attention-list"]),/Nothing requires immediate attention/);
  assert.equal(h.calls.length,8);
});

test("automatic-cache errors are visible but do not alter Plex or playlist state",async()=>{
  const h=await makeHarness({
    "/api/health":{ok:true,server:"Plex",library:"Music"},
    "/api/config/summary":{playlists:[],version:{counts:{total:0,needs_sync:0}}},
    "/api/downloads/bridge/status":{ok:true,counts:{},plex_sync:{}},
    "/api/workspace/maintenance":{enabled:true,jobs:{
      audio:{error:"AudioMuse token unavailable",pending:100},
      library:{last_success:"2026-11-11T18:00:00Z"}
    }}
  });
  assert.match(flatten(h.elements["attention-list"]),/Audio feature cache update failed/);
  assert.match(flatten(h.elements["attention-list"]),/View update details/);
  assert.match(h.elements["maintenance-status"].textContent,/100 cache records queued/);
  assert.equal(h.calls.every(path=>path.startsWith("/api/")),true);
  assert.equal(h.calls.length,4);
});


test("slow Plex response cannot block local playlist counts or maintenance state",async()=>{
  let releasePlex;
  const waiting=new Promise(resolve=>{releasePlex=resolve});
  const h=await makeHarness({
    "/api/health":waiting,
    "/api/config/summary":{version:{counts:{total:18,needs_sync:3}}},
    "/api/downloads/bridge/status":{ok:true,counts:{queued:2}},
    "/api/workspace/maintenance":{enabled:true,jobs:{audio:{},library:{}},
      automatic_analysis:{enabled:true,baseline:true,pending:2}}
  });
  assert.equal(h.elements["plex-value"].textContent,"");
  assert.equal(h.elements["playlist-count"].textContent,"18");
  assert.equal(h.elements["sync-count"].textContent,"3");
  assert.equal(h.elements["download-count"].textContent,"2");
  assert.match(h.elements["maintenance-status"].textContent,/2 albums awaiting analysis/);
  assert.match(h.elements["overview-updated"].textContent,/3\/4/);
  releasePlex({ok:true,server:"Plex",library:"Music"});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.elements["plex-value"].textContent,"Connected");

});

test("operational home identifies unindexed albums, Plex drift and missing AudioMuse features",async()=>{
  const h=await makeHarness({
    "/api/health":{ok:true,server:"Plex",library:"Music"},
    "/api/config/summary":{version:{counts:{total:6,needs_sync:0}}},
    "/api/downloads/bridge/status":{ok:true,counts:{}},
    "/api/workspace/maintenance":{enabled:true,jobs:{audio:{},library:{}},
      album_verification:{ok:true,last_checked:"2026-11-12T12:00:00Z",
        counts:{awaiting_index:2,requires_attention:1,missing_on_disk:1}},
      playlist_drift:{ok:true,checked_at:"2026-11-12T12:00:00Z",
        counts:{drift:2,missing:1}},
      audio_coverage:{cached:290,provider_available:330,pending:40}}
  });
  assert.equal(h.elements["unindexed-count"].textContent,"4");
  assert.equal(h.elements["drift-count"].textContent,"3");
  assert.match(flatten(h.elements["attention-list"]),/4 album import/);
  assert.match(flatten(h.elements["attention-list"]),/3 playlist/);
  assert.match(h.elements["maintenance-status"].textContent,/AudioMuse 290\/330 cached/);
});
