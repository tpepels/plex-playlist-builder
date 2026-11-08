import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const root = new URL("../app/", import.meta.url);
const html = fs.readFileSync(new URL("templates/_site_navigation.html", root), "utf8");
const js = fs.readFileSync(new URL("static/site-navigation.js", root), "utf8");
const css = fs.readFileSync(new URL("static/site-navigation.css", root), "utf8");
new vm.Script(js, {filename:"site-navigation.js"});

for(const route of ["/", "/downloads", "/playlists", "/playlists?view=isc",
  "/playlist-generator", "/playlist-intelligence", "/playlists?view=health",
  "/playlists?view=duplicates", "/playlists?view=newmusic",
  "/playlists?view=discover", "/musical-coherence", "/downloads-audit",
  "/api/library-export.tsv", "/api/playlists-export.yml"])
  assert(html.includes('href="'+route+'"'), "Missing route "+route);
assert(html.includes('id="workspaceTopbar"'));
assert(html.includes('id="siteNavRecentForm"'));
assert(css.includes("@media(max-width:800px)"));
assert(!html.includes("siteNavigationBackdrop"));
assert(!html.includes("siteNavigationClose"));

function harness(page="http://media-server:5051/playlist-generator") {
  const uri=new URL(page), nodes=new Map(), handlers={};
  let active;
  function node(id) {
    const attrs={}, listeners={};
    const n={id,dataset:{},hidden:false,open:false,attrs,listeners,
      setAttribute(key,value){attrs[key]=String(value)},
      removeAttribute(key){delete attrs[key]},
      getAttribute(key){return attrs[key]},
      addEventListener(key,cb){listeners[key]=cb},
      focus(){active=n},
      contains(target){return target===n},
      closest(selector){return selector===".workspace-more" && n.inMore ? more : null},
      querySelector(){return null},
    };
    nodes.set(id,n);return n;
  }
  const panel=node("siteNavigationPanel");
  const toggle=node("siteNavigationToggle");
  const more=node("workspaceMore");
  const summary=node("summary");
  more.contains=(target)=>target===more||target===summary;
  more.querySelector=(selector)=>selector==="summary"?summary:null;
  const mode=node("siteNavRecentMode"),limit=node("siteNavRecentLimit");
  const navMarkup=html.split('<nav id="siteNavigationPanel"',2)[1].split("</nav>",1)[0];
  const links=[...navMarkup.matchAll(/<a\b[^>]*href="([^"]+)"/g)].map(([,href])=>{
    const link=node("link-"+href);link.attrs.href=href;
    link.inMore=navMarkup.indexOf('href="'+href+'"')>navMarkup.indexOf('id="workspaceMore"');
    return link;
  });
  panel.querySelectorAll=selector=>selector==="a[href]"?links:[];
  panel.contains=(target)=>links.includes(target)||target===panel||target===more;
  const document={getElementById:id=>nodes.get(id)||null,
    addEventListener:(key,fn)=>handlers[key]=fn};
  vm.runInNewContext(js,{document,window:{location:uri},URL,URLSearchParams});
  return {document,panel,toggle,more,summary,mode,limit,links,handlers};
}
function click(n){assert.equal(typeof n.listeners.click,"function");n.listeners.click()}
const pages=[
  ["http://media-server:5051/","/"],
  ["http://media-server:5051/downloads","/downloads"],
  ["http://media-server:5051/playlist-generator","/playlist-generator"],
  ["http://media-server:5051/playlists?view=health","/playlists?view=health"],
  ["http://media-server:5051/playlists?view=newmusic","/playlists?view=newmusic"],
  ["http://media-server:5051/playlists?view=isc","/playlists?view=isc"],
  ["http://media-server:5051/open-youtube-album?artist=A","/playlists?view=discover"],
  ["http://media-server:5051/downloads/engine/?job=123","/downloads"],
];
for(const [url,expected] of pages){
  const h=harness(url);
  const marked=h.links.filter(n=>n.attrs["aria-current"]==="page");
  assert.equal(marked.length,1,url);
  assert.equal(marked[0].attrs.href,expected,url);
  click(h.toggle);
  assert.equal(h.panel.dataset.open,"true");
  assert.equal(h.toggle.attrs["aria-expanded"],"true");
  let prevented=false;
  h.handlers.keydown({key:"Escape",preventDefault(){prevented=true}});
  assert.equal(prevented,true);
  assert.equal(h.panel.dataset.open,"false");
  assert.equal(h.toggle.attrs["aria-expanded"],"false");
  click(h.toggle);click(marked[0]);
  assert.equal(h.panel.dataset.open,"false");
}
{
  const h=harness();
  h.more.open=true;
  let prevented=false;
  h.handlers.keydown({key:"Escape",preventDefault(){prevented=true}});
  assert(prevented);
  assert.equal(h.more.open,false);
}
{
  const h=harness();
  h.mode.value="albums";h.mode.listeners.change();
  assert.equal(h.limit.value,"20");assert.equal(h.limit.max,"200");
  h.mode.value="hours";h.mode.listeners.change();
  assert.equal(h.limit.value,"48");assert.equal(h.limit.max,"8760");
}
console.log("One workspace navigation: route highlighting, mobile menu, More and exports OK");
