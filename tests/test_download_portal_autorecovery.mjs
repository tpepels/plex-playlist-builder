import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const source=fs.readFileSync(new URL("../app/static/download-portal.js",import.meta.url),"utf8");
const html=fs.readFileSync(new URL("../app/templates/download_portal.html",import.meta.url),"utf8");
new vm.Script(source);

function harness({online=false,ready=false}={}){
  let now=500000,reloads=0,interval;
  const requests=[];
  const classes=new Set();
  const status={textContent:"",classList:{
    add:x=>classes.add(x),remove:x=>classes.delete(x),
    contains:x=>classes.has(x)
  }};
  const document={hidden:false,getElementById:id=>id==="engine-status"?status:null};
  const fetch=async(path,options)=>{
    requests.push({path,options});
    if(path==="/api/downloads/bridge/status")return {
      ok:online,status:online?200:503,json:async()=>online?
        {ok:true,counts:{queued:1}}:{ok:false,error:"yt-dlp unavailable"}
    };
    if(path==="/downloads")return {ok:true,text:async()=>ready?
      '<main id="download-form">Real downloader</main>':
      '<main>Downloader unavailable</main>'};
    throw Error("Unexpected request "+path);
  };
  const window={location:{reload(){reloads++}},
    setInterval(cb,ms){assert.equal(ms,10000);interval=cb}};
  vm.runInNewContext(source,{document,fetch,window,Date:{now:()=>now}});
  const flush=()=>new Promise(resolve=>setImmediate(resolve));
  return {status,requests,flush,tick:()=>interval(),setOnline:x=>{online=x},
    setReady:x=>{ready=x},advance:x=>{now+=x},get reloads(){return reloads}};
}

test("offline downloader recovers full interface automatically without user action",async()=>{
  const h=harness();await h.flush();
  assert.match(h.status.textContent,/retrying automatically/);
  h.setOnline(true);
  h.tick();await h.flush();
  assert.equal(h.reloads,0,"Online job API alone must not force a reload");
  h.setReady(true);h.advance(30001);
  h.tick();await h.flush();
  assert.equal(h.reloads,1);
  assert(h.requests.every(r=>!r.options.method||r.options.method==="GET"));
  assert.doesNotMatch(html,/id="scan-plex"|Retry connection/);
});

test("an already recovered page is attempted immediately on fallback render",async()=>{
  const h=harness({online:true,ready:true});
  await h.flush();
  assert.equal(h.reloads,1);
});
