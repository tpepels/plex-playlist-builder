/* Page-independent drawer. The main dashboard keeps its own in-page view controls. */
(() => {
  "use strict";
  const toggle=document.getElementById("siteNavigationToggle");
  const panel=document.getElementById("siteNavigationPanel");
  const backdrop=document.getElementById("siteNavigationBackdrop");
  const close=document.getElementById("siteNavigationClose");
  if(!toggle||!panel||!backdrop||!close)return;

  let previousFocus=null;
  const isOpen=()=>!panel.hidden;
  function setOpen(open,restore=true){
    if(open===isOpen())return;
    if(open)previousFocus=document.activeElement;
    panel.hidden=!open;
    backdrop.hidden=!open;
    toggle.setAttribute("aria-expanded",String(open));
    toggle.setAttribute("aria-label",open?"Close navigation menu":"Open navigation menu");
    document.body.classList.toggle("site-navigation-open",open);
    if(open)panel.focus();
    else if(restore)(previousFocus&&previousFocus.isConnected?
      previousFocus:toggle).focus();
  }
  toggle.addEventListener("click",()=>setOpen(!isOpen()));
  close.addEventListener("click",()=>setOpen(false));
  backdrop.addEventListener("click",()=>setOpen(false));
  document.addEventListener("keydown",event=>{
    if(!isOpen())return;
    if(event.key==="Escape"){
      event.preventDefault();
      setOpen(false);
      return;
    }
    if(event.key!=="Tab")return;
    const visible=[...panel.querySelectorAll(
      "a[href],button:not([disabled]),summary,input:not([disabled]),select:not([disabled])"
    )].filter(element=>element.getClientRects().length>0);
    if(!visible.length){
      event.preventDefault();panel.focus();return;
    }
    const first=visible[0],last=visible[visible.length-1];
    if(!visible.includes(document.activeElement)){
      event.preventDefault();(event.shiftKey?last:first).focus();
    }else if(event.shiftKey&&document.activeElement===first){
      event.preventDefault();last.focus();
    }else if(!event.shiftKey&&document.activeElement===last){
      event.preventDefault();first.focus();
    }
  });

  const mode=document.getElementById("siteNavRecentMode");
  const limit=document.getElementById("siteNavRecentLimit");
  const form=document.getElementById("siteNavRecentForm");
  if(mode&&limit){
    mode.addEventListener("change",()=>{
      if(mode.value==="albums"){
        limit.max="200";limit.value="20";
      }else{
        limit.max="8760";limit.value="48";
      }
    });
  }
  if(form)form.addEventListener("submit",()=>setOpen(false,false));

  const currentPath=window.location.pathname;
  const currentView=new URLSearchParams(window.location.search).get("view");
  // Old /?view=... URLs remain supported; the canonical dashboard is /playlists.
  const targetPath=currentPath==="/open-youtube-album"
    ?"/playlists?view=discover"
    :((currentPath==="/"||currentPath==="/playlists")&&currentView
      ?(currentView==="playlists"?"/playlists":
        "/playlists?view="+encodeURIComponent(currentView))
      :currentPath);
  for(const link of panel.querySelectorAll("a[href]")){
    const url=new URL(link.getAttribute("href"),window.location.origin);
    if(url.pathname+url.search===targetPath){
      link.setAttribute("aria-current","page");
    }
    link.addEventListener("click",()=>setOpen(false,false));
  }
})();
