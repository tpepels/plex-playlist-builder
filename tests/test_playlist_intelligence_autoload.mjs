import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const html=fs.readFileSync(new URL("../app/templates/playlist_intelligence.html",import.meta.url),"utf8");
const script=html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
assert(script,"Expected existing playlist intelligence script");
new vm.Script(script);

function harness(){
  const elements=new Map(),timers=[],calls=[],storage=new Map();
  const names=["Alpha","Beta"];
  let version="v1", playlistReads=0, namesReads=0;
  function element(tag="div"){
    const e={tagName:tag.toUpperCase(),value:"",textContent:"",className:"",hidden:false,
      disabled:false,children:[],listeners:{},checked:false,paused:true,seeking:false,
      currentTime:0,innerHTML:"",open:false,
      addEventListener(type,cb){this.listeners[type]=cb},
      replaceChildren(...children){this.children=children},
      append(...children){this.children.push(...children)},
      pause(){this.paused=true},
      play(){return Promise.resolve()},
      showModal(){this.open=true},close(){this.open=false},
      scrollIntoView(){},
    };
    return e;
  }
  for(const [,id] of html.matchAll(/\bid="([^"]+)"/g))elements.set(id,element());
  const $=id=>elements.get(id);
  $("trackFilter").value="all";$("playlist").value="";
  function source(name){
    const values={purpose:"Jazz",styles:["jazz"],moods:[],variation:"balanced",
      energy:"adaptive",flow:"smooth",live:"neutral"};
    return {name,version,snapshot:version,keys:["1","2"],playlist_id:"300",
      tracks:[{id:"1",title:"Song 1",artist:"Artist",album:"Album",energy:.4},
              {id:"2",title:"Song 2",artist:"Artist",album:"Album",energy:.5}],
      profile:{tracks:2,artists:1,coverage:1,mean_pairwise_cosine:.8,energy:.5,tempo:115,
        confidence:"limited"},
      flags:[],yaml_in_sync:true,description:"",identity:{
        values,source:"inferred",revision:"r1",evidence:{basis:["Plex"],analyzed_tracks:2}}};
  }
  const fetch=async(path,options={})=>{
    const method=options?.method||"GET";calls.push({path,method});
    if(method!=="GET")throw Error("Unexpected write "+path);
    let body;
    if(path==="/api/curation/playlists"){namesReads++;body={playlists:names.slice()}}
    else if(path.startsWith("/api/curation/playlist/")){
      playlistReads++;body=source(decodeURIComponent(path.split("/").at(-1)));
    }
    else if(path==="/api/curation/validation")body={categories:[],human_validation_complete:true};
    else if(path.startsWith("/api/curation/validation/queue?"))body={
      reviewed:0,target_review_count:0,distinct_targets:0,target_distinct:0,targets:[],audio_coverage:1};
    else if(path.startsWith("/api/curation/candidates?"))body={candidates:[]};
    else throw Error("Unexpected request "+path);
    return {ok:true,status:200,json:async()=>body};
  };
  const document={hidden:false,getElementById:$,createElement:element};
  const localStorage={getItem:key=>storage.get(key)||null,setItem:(key,val)=>storage.set(key,val)};
  const window={location:{search:""},setInterval(cb,ms){timers.push({cb,ms})}};
  const context={document,fetch,localStorage,window,URLSearchParams,
    setTimeout,clearTimeout,confirm:()=>true,console};
  vm.runInNewContext(script+"\n;globalThis.__testStage=stage;globalThis.__testActions=()=>actions.length;",context);
  return { $,calls,timers,document,context,names,
    setVersion(value){version=value},
    get playlistReads(){return playlistReads},
    get namesReads(){return namesReads},
    stage:change=>context.__testStage(change),
    actions:()=>context.__testActions()};
}
async function flush(){await new Promise(resolve=>setImmediate(resolve))}

test("opening curation selects, loads and fills the first available playlist automatically",async()=>{
  const h=harness();await flush();await flush();
  assert.equal(h.$("playlist").value,"Alpha");
  assert.equal(h.$("workspace").hidden,false);
  assert.equal(h.$("trackList").children.length,2);
  assert.match(h.$("trackCount").textContent,/2 tracks/);
  assert.match(h.$("candidateHint").textContent,/0 results/);
  assert(h.calls.some(x=>x.path==="/api/curation/playlist/Alpha"));
  assert(h.calls.some(x=>x.path.startsWith("/api/curation/candidates?")));
  assert(h.calls.some(x=>x.path==="/api/curation/validation"));
  assert(!html.includes("Reload playlist from Plex"));
  assert(!html.includes('id="refreshLibrary"'));
  assert(h.calls.every(x=>x.method==="GET"));
});

test("background updates reload when idle but preserve staged changes",async()=>{
  const h=harness();await flush();await flush();
  const check=h.timers.find(x=>x.ms===90000);
  assert(check,"Expected autonomous playlist change polling");
  const before=h.playlistReads;
  h.setVersion("v2");
  check.cb();
  await flush();await flush();
  assert.equal(h.playlistReads,before+1);
  assert.equal(h.$("workspace").hidden,false);
  h.stage({type:"remove",track_id:"2"});
  assert.equal(h.actions(),1);
  const stagedCount=h.$("changeCount").textContent;
  check.cb();
  await flush();
  assert.equal(h.playlistReads,before+1,"Staged edits must never trigger a background reload");
  assert.equal(h.$("changeCount").textContent,stagedCount);
  assert.equal(h.actions(),1);
});

test("playlist selector updates on schedule without manual reload",async()=>{
  const h=harness();await flush();await flush();
  const tick=h.timers.find(x=>x.ms===60000);
  h.names.push("Gamma");
  tick.cb();
  await flush();await flush();
  assert(h.$("playlist").children.some(x=>x.value==="Gamma"));
  assert.equal(h.$("playlist").value,"Alpha");
});

test("candidate search reacts to typing without requiring a Search button",async()=>{
  const h=harness();await flush();await flush();
  const before=h.calls.filter(x=>x.path.startsWith("/api/curation/candidates?")).length;
  h.$("candidateSearch").value="Miles";
  h.$("candidateSearch").oninput();
  await new Promise(resolve=>setTimeout(resolve,280));
  await flush();
  const searches=h.calls.filter(x=>x.path.startsWith("/api/curation/candidates?"));
  assert.equal(searches.length,before+1);
  assert.match(searches.at(-1).path,/q=Miles/);
  assert(!html.includes('id="searchCandidates"'));
});

test("editing musical identity prevents background data from overwriting inputs",async()=>{
  const h=harness();await flush();await flush();
  h.$("intentPurpose").value="My unsaved musical aim";
  h.$("intentPurpose").listeners.input();
  h.setVersion("v9");
  const before=h.playlistReads;
  h.timers.find(x=>x.ms===90000).cb();
  await flush();
  assert.equal(h.playlistReads,before);
  assert.equal(h.$("intentPurpose").value,"My unsaved musical aim");
});
