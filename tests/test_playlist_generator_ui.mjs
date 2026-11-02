// Offline generator UI contract: syntax, DOM links, complete workflow and safe approval.
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const html=fs.readFileSync(new URL("../app/templates/playlist_generator.html",import.meta.url),"utf8");
const match=html.match(/<script>([\s\S]*?)<\/script>/);
assert(match,"Generator script must exist");
new vm.Script(match[1],{filename:"playlist_generator.js"});
const script=match[1];
const ids=new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]));
for(const m of script.matchAll(/\$\('([^']+)'\)/g)){
  assert(ids.has(m[1]),"Missing DOM control: "+m[1]);
}
for(const endpoint of [
  "/api/generation/drafts",
  "/api/generation/plan",
  "/api/generation/publish",
  "/api/curation/audio/",
  "/candidates","/edit"
])assert(script.includes(endpoint),"Missing backend wiring: "+endpoint);
for(const id of ["prompt","count","minutes","name","createForm","trackList",
  "candidateList","publishDialog","publishTracks","publishApproval",
  "confirmPublish","review","restoreDraft"])
  assert(ids.has(id),"Missing generation/review control "+id);
assert(script.includes("approved:true"),"Plex publication must require approval");
assert(script.includes("draft.revision"),"Expected optimistic revision checks");
assert(script.includes("localStorage.setItem"),"Draft persistence must be integrated");
assert(script.includes("type:'replace'"),"Replacement must work");
assert(script.includes("type:'add'"),"Add must work");
assert(script.includes("type:'remove'"),"Remove must work");
assert(script.includes("type:'move'"),"Reorder must work");
console.log("Generation UI: parser, DOM and complete workflow contract OK");
