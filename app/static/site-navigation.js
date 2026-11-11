/* The one workspace navigation. No page-specific drawers, tab navigation or modal menu. */
(() => {
  "use strict";
  const panel = document.getElementById("siteNavigationPanel");
  const toggle = document.getElementById("siteNavigationToggle");
  const more = document.getElementById("workspaceMore");
  const playlistMenu = document.getElementById("workspacePlaylistMenu");
  if (!panel || !toggle || !more) return;

  const links = Array.from(panel.querySelectorAll("a[href]"));
  const current = new URL(window.location.href);
  // Secondary workflows are entered from the Playlists & library workspace.
  // Highlight that parent instead of suggesting a separate top-level section.
  let currentRoute = current.pathname;
  if (current.pathname === "/open-youtube-album" ||
      current.pathname === "/playlist-generator" ||
      current.pathname === "/playlist-intelligence" ||
      current.pathname === "/playlists" ||
      (current.pathname === "/" && new URLSearchParams(current.search).has("view"))) {
    currentRoute = "/playlists";
  } else if (current.pathname === "/downloads/engine" ||
             current.pathname.startsWith("/downloads/engine/")) {
    currentRoute = "/downloads";
  }
  let moreActive = false;
  let activeClaimed = false;
  for (const link of links) {
    const href = link.getAttribute("href");
    if (href === currentRoute && !activeClaimed) {
      activeClaimed = true;
      link.setAttribute("aria-current", "page");
      if (link.closest(".workspace-more")) moreActive = true;
    } else {
      link.removeAttribute("aria-current");
    }
  }
  if (moreActive) more.querySelector("summary")?.setAttribute("aria-current", "page");

  function closeMobile(restore = false) {
    panel.dataset.open = "false";
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-label", "Open workspace navigation");
    if (restore) toggle.focus();
  }
  toggle.addEventListener("click", () => {
    const expanded = panel.dataset.open !== "true";
    panel.dataset.open = String(expanded);
    toggle.setAttribute("aria-expanded", String(expanded));
    toggle.setAttribute("aria-label", expanded
      ? "Close workspace navigation" : "Open workspace navigation");
  });
  for (const link of links) link.addEventListener("click", () => {
    closeMobile();
    more.open = false;
    if (playlistMenu) playlistMenu.open = false;
  });
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    if (playlistMenu?.open) {
      playlistMenu.open = false;
      playlistMenu.querySelector("summary")?.focus();
      event.preventDefault();
    } else if (more.open) {
      more.open = false;
      more.querySelector("summary")?.focus();
      event.preventDefault();
    } else if (panel.dataset.open === "true") {
      closeMobile(true);
      event.preventDefault();
    }
  });
  document.addEventListener("click", event => {
    if (more.open && !more.contains(event.target)) more.open = false;
    if (playlistMenu?.open && !playlistMenu.contains(event.target))
      playlistMenu.open = false;
    if (panel.dataset.open === "true" && !panel.contains(event.target)
        && !toggle.contains(event.target)) closeMobile();
  });

  playlistMenu?.addEventListener("toggle", () => {
    if (playlistMenu.open) more.open = false;
  });
  more.addEventListener("toggle", () => {
    if (more.open && playlistMenu) playlistMenu.open = false;
  });
  const mode = document.getElementById("siteNavRecentMode");
  const limit = document.getElementById("siteNavRecentLimit");
  if (mode && limit) mode.addEventListener("change", () => {
    const albums = mode.value === "albums";
    limit.value = albums ? "20" : "48";
    limit.max = albums ? "200" : "8760";
    limit.setAttribute("aria-label", albums ? "Number of albums" : "Number of hours");
  });
})();
