// Fast, offline route-to-UI coverage, no browser dependencies.
'use strict';
const fs=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const src=fs.readFileSync('app/templates/playlist_intelligence.html','utf8');
const scripts=[...src.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m=>m[1]);
assert.equal(scripts.length,1);
new vm.Script(scripts[0],{filename:'playlist_intelligence.js'});
const script=scripts[0];
const ids=new Set([...src.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]));
for(const m of script.matchAll(/\$\('([^']+)'\)/g)){
  assert(ids.has(m[1]),'Unknown UI element: '+m[1]);
}
for(const path of [
  '/api/curation/playlists','/api/curation/playlist/',
  '/api/curation/candidates','/api/curation/plan','/api/curation/apply',
  '/api/curation/audio/','/api/curation/library/refresh'
]) assert(script.includes(path),'Missing integration: '+path);
for(const func of ['chooseTarget','renderTracks','stage','listen','renderDraft']){
  assert(script.includes('function '+func+'('),'Missing function: '+func);
}
for(const id of ['intentPurpose','intentStyles','intentMoods','intentVariation',
                  'intentEnergy','intentFlow','intentLive','saveIdentity','resetIdentity']) {
  assert(ids.has(id),'Missing identity editor field: '+id);
}
assert(script.includes('/api/curation/identity/'),'Identity persistence not integrated');
assert(script.includes('after_id:t.suggested_after_id'),'Missing context-aware insertion');
assert(script.includes('t.confidence'),'Missing candidate confidence');
assert(script.includes('approved:true'),'Explicit approval required.');
assert(src.includes('<dialog id="approval"'),'Missing preview dialog.');
console.log('Playlist Intelligence UI: syntax, DOM and API wiring OK');
