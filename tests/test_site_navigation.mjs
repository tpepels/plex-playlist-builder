import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const root=new URL("../app/",import.meta.url);
const html=fs.readFileSync(new URL("templates/_site_navigation.html",root),"utf8");
const js=fs.readFileSync(new URL("static/site-navigation.js",root),"utf8");
const css=fs.readFileSync(new URL("static/site-navigation.css",root),"utf8");
new vm.Script(js,{filename:"site-navigation.js"});

for(const route of ["/?view=playlists","/?view=health","/?view=duplicates",
  "/?view=newmusic","/?view=isc","/?view=discover","/playlist-generator",
  "/playlist-intelligence","/musical-coherence","/downloads-audit"])
  assert(html.includes('href="'+route+'"'),"Missing destination "+route);
assert(css.includes("@media(max-width:680px)"),"Mobile navigation styling missing");

function harness(url="http://media-server:5051/playlist-generator"){
  const uri=new URL(url),all=new Map();
  const classes=new Set();
  const document={
    activeElement:null,
    body:{classList:{toggle(name,enabled){
      if(enabled)classes.add(name);else classes.delete(name);
    }}},
    handlers:{},
    getElementById(id){return all.get(id)||null},
    addEventListener(event,callback){this.handlers[event]=callback},
  };
  function element(id){
    const e={id,hidden:id!=="siteNavigationToggle",isConnected:true,
      listeners:{},attrs:{},rects:[{}],
      addEventListener(name,callback){this.listeners[name]=callback},
      setAttribute(name,value){this.attrs[name]=String(value)},
      getAttribute(name){return this.attrs[name]},
      focus(){document.activeElement=this},
      getClientRects(){return this.rects},
    };
    all.set(id,e);
    return e;
  }
  const toggle=element("siteNavigationToggle"),panel=element("siteNavigationPanel");
  const backdrop=element("siteNavigationBackdrop"),close=element("siteNavigationClose");
  toggle.hidden=false;close.hidden=false;
  const mode=element("siteNavRecentMode"),limit=element("siteNavRecentLimit");
  const form=element("siteNavRecentForm");
  const links=[...html.matchAll(/<a href="([^"]+)"/g)].map(([,href])=>{
    const e=element("link-"+href);
    e.attrs.href=href;
    return e;
  });
  panel.querySelectorAll=(selector)=>{
    if(selector==="a[href]")return links;
    return [close,...links,mode,limit];
  };
  document.activeElement=toggle;
  vm.runInNewContext(js,{document,
    window:{location:{pathname:uri.pathname,search:uri.search,
      origin:uri.origin}},URL,URLSearchParams});
  return {document,toggle,panel,backdrop,close,mode,limit,form,links,classes};
}

function click(e){assert.equal(typeof e.listeners.click,"function");e.listeners.click()}
function key(h,key,shiftKey=false){
  let prevented=false;
  h.document.handlers.keydown({key,shiftKey,preventDefault(){prevented=true}});
  return prevented;
}

const pages=[
  "http://media-server:5051/playlist-generator",
  "http://media-server:5051/playlist-intelligence",
  "http://media-server:5051/musical-coherence",
  "http://media-server:5051/downloads-audit",
  "http://media-server:5051/open-youtube-album?artist=A&album=B",
  "http://media-server:5051/?view=health"
];
for(const url of pages){
  const h=harness(url);
  const expected=new URL(url).pathname==="/open-youtube-album"?"/?view=discover":
    new URL(url).pathname+new URL(url).search;
  const matches=h.links.filter(l=>l.attrs.href===expected);
  assert.equal(matches.length,1,"Missing current page marker "+url);
  assert.equal(matches[0].attrs["aria-current"],"page");
  click(h.toggle);
  assert.equal(h.panel.hidden,false);
  assert.equal(h.backdrop.hidden,false);
  assert.equal(h.toggle.attrs["aria-expanded"],"true");
  assert(h.classes.has("site-navigation-open"));
  assert.equal(key(h,"Escape"),true);
  assert.equal(h.panel.hidden,true);
  assert.equal(h.toggle.attrs["aria-expanded"],"false");
  assert.equal(h.document.activeElement,h.toggle);
  click(h.toggle);click(h.backdrop);
  assert.equal(h.panel.hidden,true);
  click(h.toggle);click(h.close);
  assert.equal(h.panel.hidden,true);
}
{
  const h=harness();
  click(h.toggle);
  h.document.activeElement=h.panel;
  assert.equal(key(h,"Tab",true),true);
  assert.equal(h.document.activeElement,h.limit);
  h.document.activeElement=h.limit;
  assert.equal(key(h,"Tab"),true);
  assert.equal(h.document.activeElement,h.close);
  h.document.activeElement=h.close;
  assert.equal(key(h,"Tab",true),true);
  assert.equal(h.document.activeElement,h.limit);
}
{
  const h=harness();
  assert.equal(typeof h.mode.listeners.change,"function");
  h.mode.value="albums";h.mode.listeners.change();
  assert.equal(h.limit.value,"20");assert.equal(h.limit.max,"200");
  h.mode.value="hours";h.mode.listeners.change();
  assert.equal(h.limit.value,"48");assert.equal(h.limit.max,"8760");
  click(h.toggle);h.form.listeners.submit();
  assert.equal(h.panel.hidden,true);
}
console.log("Shared site navigation: all routes, active state, keyboard, backdrop, focus and exports OK");
