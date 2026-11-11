import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const template=fs.readFileSync(new URL("../app/templates/playlist_generator.html",import.meta.url),"utf8");
const script=template.match(/<script>([\s\S]*?)<\/script>/)?.[1];
assert(script);
new vm.Script(script,{filename:"playlist_generator.js"});
const ids=[...template.matchAll(/\bid="([^"]+)"/g)].map(x=>x[1]);

function harness({readinessError=false}={}){
  const elements=new Map();
  function create(tag="div",id=""){
    const listeners={};
    const attrs={};
    const x={
      id,tagName:tag.toUpperCase(),value:"",textContent:"",className:"",
      children:[],hidden:false,disabled:false,paused:true,seeking:false,
      checked:false,currentTime:0,attrs,listeners,
      addEventListener:(name,cb)=>{listeners[name]=cb},
      getAttribute:name=>attrs[name]??null,
      setAttribute:(name,value)=>{attrs[name]=String(value)},
      removeAttribute:name=>{delete attrs[name]},
      replaceChildren(...items){x.children=[...items]},
      append(...items){x.children.push(...items)},
      pause(){x.paused=true},
      play(){x.paused=false;return Promise.resolve()},
      showModal(){x.open=true},
      close(){x.open=false},
    };
    if(tag==="table")x.tBodies=[create("tbody")];
    return x;
  }
  for(const id of ids)elements.set(id,create("div",id));
  const get=id=>elements.get(id);
  Object.entries({
    prompt:"Hard bop jazz with balanced artist variety and smooth transitions.",
    genre:"jazz",subgenre:"Hard bop",count:"30",name:"Test Hard Bop",
    variation:"balanced",flow:"smooth",energy:"",live:"",moods:"",
    styles:"",minutes:"",yearFrom:"",yearTo:"",
    evidenceThreshold:"balanced",excludedArtists:"",excludedAlbums:"",
    excludedTracks:"",referenceQuery:"Blue Note",
  }).forEach(([id,value])=>{if(get(id))get(id).value=value});
  for(const id of ["quality","publicationQuality"])
    get(id).tagName="TABLE",get(id).tBodies=[create("tbody")];

  const tracks=Array.from({length:30},(_,i)=>({
    id:String(i+1),artist:"Band "+i,album:"Session "+i,title:"Hard bop "+i,
    duration_ms:350000,year:1960,genre_evidence:{
      compatible:true,fit:.93,
      evidence:{metadata_support:"broad_genre",
        musicnn_similarity:.96,clap_similarity:.92,subgenre_contrast:.32,
        reference_support:"acoustic_reference_comparison",review_status:"unreviewed"},
    },
  }));
  const metrics={};
  for(const field of ["coherence","variation","energy","flow","intent"])
    metrics[field]={score:.78,coverage:1,observed:30,total:30,status:"scored"};
  const draftBase={
    id:"generated-42",revision:1,status:"draft",
    source_stale:{library:false},
    data:{
      name:"Test Hard Bop",keys:tracks.map(x=>x.id),
      tracks,request:{
        prompt:"Hard bop jazz with balanced artist variety and smooth transitions.",
        identity:{styles:["jazz"],moods:[],variation:"balanced",flow:"smooth",
          energy:"medium",live:"neutral"},
        criteria:{count:30,target_minutes:null,genre:"jazz",subgenre:"hard bop",
          reference_track_ids:[],negative_reference_track_ids:[],excluded_track_ids:[],
          years:null,evidence_threshold:"balanced"},
      },
      diagnostics:{tracks:30,duration_minutes:175,unique_artists:30,
        unique_albums:30,analyzed:30,metrics,flags:[],transitions:[]},
      selection:{catalogue:400,initial_candidates:400,eligible:80,
        shortlisted:80,excluded:{}},
      warnings:[],genre_profile:{verified_metadata_count:4,
        manual_positive_count:0,manual_negative_count:0},
      genre_evidence:Object.fromEntries(tracks.map(x=>[x.id,x.genre_evidence])),
      reviews:{},
    }
  };
  let draft=structuredClone(draftBase);
  const calls=[];
  const candidate={id:"31",artist:"Band extra",album:"Extra Session",title:"Extra bop",
    match:.83,confidence:"high",suggested_after_id:"10",
    genre_evidence:tracks[0].genre_evidence};
  const respond=async(path,opts={})=>{
    const method=opts.method||"GET",body=opts.body?JSON.parse(opts.body):null;
    calls.push({path,method,body});
    if(readinessError&&path==="/api/generation/readiness")
      return {ok:false,status:503,json:async()=>({detail:"Plex unavailable"})};
    let value;
    if(path==="/api/generation/readiness")
      value={plex_tracks:400,matched_track_ids:400,musicnn_available:400,
        clap_available:400,pending:0,ready:true};
    else if(path.startsWith("/api/generation/genre-profiles"))
      value={profiles:[{analyzed_references:4,available:true}]};
    else if(path.startsWith("/api/generation/reference-search"))
      value={tracks:[{id:"200",artist:"Reference Band",album:"Reference LP",
        title:"Reference Track",album_track_ids:["200","201"]}]};
    else if(path==="/api/generation/drafts"&&method==="POST")value=draft;
    else if(path.startsWith("/api/generation/drafts/")&&path.includes("/candidates?")){
      value={revision:draft.revision,candidates:draft.data.reviews[candidate.id]?.verdict==="reject"?[]:[candidate]};
    } else if(path.endsWith("/reviews")&&method==="POST"){
      draft=structuredClone(draft);draft.revision++;
      draft.data.reviews[body.candidate_id]={verdict:body.verdict,note:body.note};
      value=draft;
    } else if(path.endsWith("/regenerate")&&method==="POST"){
      draft=structuredClone(draft);draft.revision++;
      value=draft;
    } else if(path==="/api/generation/plan"){
      value={name:body.name,tracks:30,minutes:175,plan_id:"plan-abc",
        warnings:[],quality:{metrics},track_order:tracks};
    } else if(path==="/api/generation/publish"){
      draft=structuredClone(draft);draft.status="published";draft.playlist_id="900";
      value={status:"published",sync_state:"synced",tracks:30,playlist_id:"900"};
    } else if(path.startsWith("/api/generation/drafts/")&&method==="GET"){
      value=draft;
    } else throw Error("Unexpected mock fetch "+method+" "+path);
    return {ok:true,status:200,json:async()=>value};
  };
  const storage=new Map();
  const localStorage={getItem:key=>storage.get(key)||null,
    setItem:(key,value)=>storage.set(key,value),
    removeItem:key=>storage.delete(key)};
  const context=vm.createContext({
    document:{getElementById:id=>get(id)||null,createElement:tag=>create(tag)},
    fetch:respond,localStorage,confirm:()=>true,
    setTimeout,clearTimeout,encodeURIComponent,Number,Promise,Set,Map
  });
  vm.runInContext(script,context);
  return {get,calls,context,storage};
}
async function settle(){await new Promise(resolve=>setImmediate(resolve));}

test("Hard bop generator submits authoritative structured controls and references",async()=>{
  const h=harness();
  await settle();
  assert.match(h.get("generationReadinessText").textContent,/Ready/);
  h.get("hardBopPreset").onclick();
  h.get("referenceQuery").value="Blue Note";
  h.get("referenceQuery").onkeydown({key:"Enter",preventDefault(){}});
  await settle();
  const row=h.get("referenceSearchResults").children[0];
  assert(row?.children?.length>=5);
  row.children[1].onclick(); // positive track
  row.children[4].onclick(); // negative album (moves same track to negative)
  row.children[3].onclick(); // positive album
  await h.get("createForm").onsubmit({preventDefault(){}});
  const call=h.calls.find(c=>c.path==="/api/generation/drafts");
  assert(call);
  assert.equal(call.body.genre,"jazz");
  assert.equal(call.body.subgenre,"Hard bop");
  assert.equal(call.body.count,30);
  assert.equal(call.body.identity.variation,"balanced");
  assert.equal(call.body.identity.flow,"smooth");
  assert.deepEqual(Array.from(call.body.reference_track_ids),["200","201"]);
  assert.deepEqual(Array.from(call.body.negative_reference_track_ids),[]);
  assert.equal(h.get("workspace").hidden,false);
  assert.equal(h.get("trackList").children.length,30, h.get("notice").textContent);
  assert.match(h.get("selection").textContent,/400 library tracks/);
  assert.equal(h.get("review").disabled,false);
  assert(!h.calls.some(c=>c.path==="/api/generation/publish"));
});

test("Review rejection is versioned and prevents automatic repeated suggestion",async()=>{
  const h=harness();
  await h.get("createForm").onsubmit({preventDefault(){}});
  await settle(); // Candidate list now fills automatically with the created draft.
  const list=h.get("candidateList");
  assert.equal(list.children.length,1, h.get("notice").textContent);
  const reviewButton=list.children[0].children.find(
    x=>x.textContent==="Review");
  assert(reviewButton);
  reviewButton.onclick();
  assert.equal(h.get("generatorReviewDialog").open,true);
  assert.equal(h.get("generatorCandidateAudio").src,
    "/api/curation/audio/31");
  // Needs-review is auditable without pretending playback was completed.
  await h.get("generatorReviewLater").onclick();
  assert.equal(h.get("generatorReviewDialog").open,false);
  const call=h.calls.find(c=>c.path.endsWith("/reviews"));
  assert.equal(call.body.verdict,"needs_review");
  assert.equal(call.body.candidate_id,"31");
  assert.equal(call.body.revision,1);
  assert(!h.calls.some(c=>c.path==="/api/generation/publish"));
});

test("Publish requires review dialog and affirmative user consent",async()=>{
  const h=harness();
  await h.get("createForm").onsubmit({preventDefault(){}});
  await h.get("review").onclick();
  assert.equal(h.get("publishDialog").open,true,h.get("notice").textContent);
  assert.equal(h.get("confirmPublish").disabled,true);
  assert(!h.calls.some(c=>c.path==="/api/generation/publish"));
  h.get("publishApproval").checked=true;
  h.get("publishApproval").onchange();
  assert.equal(h.get("confirmPublish").disabled,false);
  await h.get("confirmPublish").onclick();
  const publish=h.calls.find(c=>c.path==="/api/generation/publish");
  assert.equal(publish.body.approved,true);
  assert.equal(publish.body.plan_id,"plan-abc");
  assert.equal(h.get("publishDialog").open,false);
});

test("Plex outage reports readiness failure but preserves controls",async()=>{
  const h=harness({readinessError:true});
  await settle();
  assert.match(h.get("generationReadinessText").textContent,/Plex unavailable/);
  assert.equal(h.get("generate").disabled,false);
  assert.equal(h.get("genre").value,"jazz");
});

test("Explicit regeneration is revisioned, never silently triggered",async()=>{
  const h=harness();
  await h.get("createForm").onsubmit({preventDefault(){}});
  assert(!h.calls.some(c=>c.path.endsWith("/regenerate")));
  await h.get("regenerateDraft").onclick();
  const call=h.calls.find(c=>c.path.endsWith("/regenerate"));
  assert(call);
  assert.equal(call.body.revision,1);
  assert.match(h.get("draftStatus").textContent,/revision 2/);
});

test("An existing unpublished draft can be reopened by ID without generating or publishing",async()=>{
  const h=harness();
  h.get("existingDraftId").value="generated-42";
  h.get("openExistingDraft").onclick();
  await settle();
  const loaded=h.calls.find(c=>c.path==="/api/generation/drafts/generated-42"&&c.method==="GET");
  assert(loaded);
  assert.equal(h.get("workspace").hidden,false);
  assert.equal(h.get("trackList").children.length,30);
  assert(!h.calls.some(c=>c.path==="/api/generation/publish"));
});


test("structured music selection works without outdated description text",async()=>{
  const h=harness();
  h.get("hardBopPreset").onclick();
  assert.equal(h.get("prompt").value,"");
  h.get("genre").value="soul";
  h.get("genre").onchange();
  assert.equal(h.get("subgenre").value,"");
  await h.get("createForm").onsubmit({preventDefault(){}});
  const created=h.calls.find(c=>c.path==="/api/generation/drafts");
  assert(created);
  assert.equal(created.body.genre,"soul");
  assert.equal(created.body.subgenre,null);
  assert.equal(created.body.prompt,"");
  assert.equal(created.body.identity.variation,"balanced");
  assert.equal(created.body.identity.flow,"smooth");
  assert(!h.calls.some(c=>c.path==="/api/generation/publish"));
});

test("generator puts optional descriptions in advanced controls",()=>{
  assert.match(template, /Optional description and advanced preferences/);
  assert.doesNotMatch(template, /<textarea[^>]*id="prompt"[^>]*required/);
  assert.match(template, /<select id="variation"/);
  assert.match(template, /<select id="flow"/);
});
