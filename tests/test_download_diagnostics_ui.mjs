import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const template=fs.readFileSync(new URL("../app/templates/disk_audit.html",import.meta.url),"utf8");
const script=template.match(/<script>\s*([\s\S]*?)<\/script>/)?.[1]
  .replace("{{ downloader_url | tojson }}",JSON.stringify("/downloads"));
assert(script,"Expected inline disk reconciliation script");
new vm.Script(script);

function element(id=""){
  return {id,children:[],textContent:"",value:"",hidden:false,disabled:false,
    attrs:{},listeners:{},
    append(...nodes){this.children.push(...nodes);
      if(this.id==="filter"&&!this.value&&nodes.length)this.value=nodes[0].value;},
    replaceChildren(...nodes){this.children=nodes;
      if(this.id==="filter")this.value=nodes[0]?.value||"";},
    setAttribute(k,v){this.attrs[k]=String(v)},
    addEventListener(k,v){this.listeners[k]=v},
  };
}
function harness(search="?tab=history&q=job-123"){
  const nodes=new Map(), calls=[],history=[];
  const get=id=>{if(!nodes.has(id))nodes.set(id,element(id));return nodes.get(id)};
  const document={
    hidden:false,getElementById:get,createElement:tag=>element(tag),
    addEventListener(k,cb){this[k]=cb}
  };
  const location={search};
  const fetch=async(url)=>{
    const path=String(url);calls.push(path);
    if(path.startsWith("/api/download-history?"))return {ok:true,json:async()=>({
      total:1,items:[{job_id:"job-123",artist:"Bill Evans",title:"Waltz for Debby",
        status:"failed",completed_items:2,total_items:4,reason:"HTTP 403",
        final_dir:"",url:"",removed_from_queue:false,deleted:false}],
      limitation:""
    })};
    if(path.startsWith("/api/disk-reconciliation?"))return {ok:true,json:async()=>({
      disk_album_folders:1,disk_audio_files:2,not_in_plex_audio_files:2,
      not_in_plex_album_folders:1,disk_status_counts:{missing:1},
      staging:{audio_files:0},disk_scan_complete:true,plex_total_media_files:1,
      plex_music_locations:["/music"],caveats:"File paths compared.",
      scan_errors:[],total_matches:1,items:[{
        status:"missing",artist_folder:"Bill Evans",album_folder:"Waltz for Debby",
        folder:"/music/Bill Evans/Waltz for Debby",files_not_in_plex:2,
        files_indexed:0,files_on_disk:2,size_bytes:123,plex_labels:[],missing_files:[]
      }]
    })};
    throw Error("Unexpected fetch "+path);
  };
  const window={setInterval(){}};
  const context={document,window,location,history:{replaceState(_s,_t,url){
    history.push(String(url));
  }},fetch,URLSearchParams,encodeURIComponent,setTimeout,clearTimeout};
  vm.runInNewContext(script,context,{timeout:2000});
  return {get,calls,history};
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));

test("job deep link opens history automatically and searches the exact job",async()=>{
  const h=harness();
  await flush();
  assert(h.calls[0].startsWith("/api/download-history?"),h.calls[0]);
  assert.match(h.calls[0],/q=job-123/);
  assert.equal(h.get("history-tab").attrs["aria-selected"],"true");
  assert.equal(h.get("disk-tab").attrs["aria-selected"],"false");
  assert.match(h.get("records").children[0].children[0].textContent,/Bill Evans/);
  assert(h.history.some(x=>x.includes("tab=history")));
});

test("missing album page loads disk comparison on entry without a button",async()=>{
  const h=harness("?q=Waltz%20for%20Debby");
  await flush();
  assert(h.calls[0].startsWith("/api/disk-reconciliation?"));
  assert.match(h.calls[0],/q=Waltz/);
  assert.match(h.get("records").children[0].children[0].textContent,/Waltz for Debby/);
  assert.match(template,/Albums missing from Plex/);
  assert.match(template,/Search artist, album, job ID or file/);
  assert(!template.includes('id="check"'));
});
