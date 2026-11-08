"""Compose the downloader's complete native interface into the music workspace.

The yt-dlp service owns its markup, forms, jobs, cookies and VPN JavaScript.
The Playlist Manager owns the surrounding navigation and library actions.
This is one document and one scroll surface, never an iframe or a second UI.
"""
from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import Request

from .downloader_bridge import PREFIX, downloader_origin

_MAX_HTML_BYTES = 4 * 1024 * 1024
_NAVIGATION = Path(__file__).parent / "templates" / "_site_navigation.html"
_HEADER = '<header class="page-head signal-header signal-transition">'
_BRAND = '<div class="signal-brand">'
_TOOLS = '<div class="signal-tools">'
_HEAD_END = "</head>"
_BODY_END = "</body>"

_PRIMARY_NAV = """<nav class="download-native-primary" aria-label="Primary navigation">
  <a href="/">Overview</a>
  <a href="/downloads" aria-current="page">Downloads</a>
  <a href="/playlists">Playlists</a>
  <a href="/playlist-intelligence">Curate</a>
</nav>"""

_NAV_BUTTON = """<button id="siteNavigationToggle" class="site-nav-toggle" type="button"
 aria-label="Open navigation menu" aria-expanded="false"
 aria-controls="siteNavigationPanel" title="Navigation">
 <span class="site-nav-lines" aria-hidden="true"><span></span><span></span><span></span></span>
</button>"""

_ACTIONS = """<nav class="download-native-actions" aria-label="Download and library actions">
  <button id="scan-plex" type="button">Scan Plex for new music</button>
  <a href="/playlists?view=newmusic">Review new music</a>
  <a href="/playlist-intelligence">Add tracks to playlists</a>
  <a href="/downloads-audit">Import audit</a>
</nav>
<div id="engine-status" class="download-native-status" role="status" aria-live="polite">Checking download engine…</div>
<div id="scan-result" class="download-native-result" role="status" aria-live="polite"></div>"""

_ASSETS = """<link rel="stylesheet" href="/static/site-navigation.css">
<link rel="stylesheet" href="/static/download-native.css">"""

_SCRIPTS = """<script src="/static/site-navigation.js" defer></script>
<script src="/static/download-portal.js" defer></script>"""


class DownloadPageError(RuntimeError):
    """The downloader document cannot safely be composed."""


def compose_native_page(html: str) -> str:
    """Insert shared navigation into the native downloader HTML, preserving its scripts.

    Fail closed on changed upstream markup rather than silently drop controls.
    The source is the configured internal downloader, never an arbitrary URL.
    """
    required = (_HEADER, _BRAND, _TOOLS, _HEAD_END, _BODY_END,
                '<main class="signal-shell">', "</header>",
                '<meta name="downloader-base"', 'id="download-form"',
                'id="jobs"', 'id="vpn-panel"', 'id="youtube-cookie-panel"')
    if (any(html.count(marker) != 1 for marker in required)
            or 'content="' + PREFIX + '"' not in html
            or "<iframe" in html.lower()
            or 'id="siteNavigationPanel"' in html):
        raise DownloadPageError("Download engine HTML structure changed; refusing an incomplete interface")

    navigation = _NAVIGATION.read_text(encoding="utf-8")
    html = html.replace("<title>yt-dlp music server</title>",
                        "<title>Downloads · Music workspace</title>", 1)
    html = html.replace(_HEAD_END, _ASSETS + "\n" + _HEAD_END, 1)
    html = html.replace(_BRAND, _BRAND + "\n" + _NAV_BUTTON, 1)
    html = html.replace(_TOOLS, _TOOLS + "\n" + _PRIMARY_NAV, 1)
    html = html.replace("SIGNAL CONSOLE / AUDIO OPERATIONS",
                        "MUSIC WORKSPACE / AUDIO IMPORTS", 1)
    html = html.replace("DOWNLOAD <span>/ CONTROL</span>",
                        "Downloads", 1)
    html = html.replace(
        "</header>",
        "</header>\n" + navigation + "\n" + _ACTIONS, 1,
    )
    html = html.replace(_BODY_END, _SCRIPTS + "\n" + _BODY_END, 1)
    return html


async def fetch_native_page(request: Request) -> str:
    """Retrieve a bounded, server-rendered document using fixed Docker DNS."""
    if len(request.url.query) > 2048:
        raise DownloadPageError("Download query is too long")
    headers = {
        "accept": "text/html",
        "accept-encoding": "identity",
        "x-forwarded-prefix": PREFIX,
        "x-forwarded-host": request.headers.get("host", ""),
        "x-forwarded-proto": request.url.scheme,
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=5),
                                 follow_redirects=False) as client:
        async with client.stream(
            "GET", downloader_origin() + "/",
            params=list(request.query_params.multi_items()),
            headers=headers,
        ) as response:
            if response.status_code != 200:
                raise DownloadPageError(
                    "Download engine did not return its interface (HTTP %d)" %
                    response.status_code
                )
            if "text/html" not in response.headers.get("content-type", ""):
                raise DownloadPageError("Download engine returned a non-HTML document")
            chunks = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > _MAX_HTML_BYTES:
                    raise DownloadPageError("Download engine HTML exceeded the size limit")
                chunks.append(chunk)
    try:
        markup = b"".join(chunks).decode("utf-8")
    except UnicodeError as exc:
        raise DownloadPageError("Download engine HTML is not UTF-8") from exc
    return compose_native_page(markup)
