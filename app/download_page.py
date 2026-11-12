"""Integrate the native downloader into the single Music workspace shell.

The yt-dlp container retains its HTML, form controls and JavaScript. Only its
separate top header is replaced by the identical navigation used everywhere.
"""
from __future__ import annotations

from pathlib import Path
import httpx
from fastapi import Request
from .downloader_bridge import PREFIX, downloader_origin

_MAX_HTML_BYTES = 4 * 1024 * 1024
_NAVIGATION = Path(__file__).parent / "templates" / "_site_navigation.html"
_HEADER = '<header class="page-head signal-header signal-transition">'
_HEAD_END = "</head>"
_BODY_END = "</body>"

# All commands and backend actions remain inside the native download form.
# These are contextual actions, not a second page navigation menu.
_ACTIONS = """<div class="workspace-page-heading">
  <span class="workspace-page-label">MUSIC IMPORT</span>
  <h1>Downloads</h1>
  <p>Add a source or manage the queue, retries and completed imports.</p>
</div>
<div class="download-native-actions">
  <a href="/downloads-audit" class="download-reconciliation-link">Find albums missing from Plex</a>
  <a href="/downloads-audit?tab=history">Download history and errors</a>
  <a href="/downloads/engine/chrome-extension.zip" download>Chrome extension</a>
</div>"""

_ASSETS = """<link rel="stylesheet" href="/static/site-navigation.css">
<link rel="stylesheet" href="/static/download-native.css">"""
# The offline recovery script reloads the page when the native form returns.
# Never load it on a successful native document: it would detect its *own*
# download form on every poll and reload the entire page indefinitely.
_SCRIPTS = """<script src="/static/site-navigation.js" defer></script>"""


class DownloadPageError(RuntimeError):
    """The downloader document cannot safely be composed."""


def compose_native_page(html: str) -> str:
    """Replace only the native page header; preserve all functional markup.

    Reject a changed upstream shape rather than losing a form or a VPN action.
    """
    required = (
        _HEADER, _HEAD_END, _BODY_END, '<main class="signal-shell">',
        '<meta name="downloader-base"', 'id="download-form"', 'id="jobs"',
        'id="vpn-panel"', 'id="youtube-cookie-panel"', 'id="history-results"',
    )
    if (any(html.count(marker) != 1 for marker in required)
            or 'content="' + PREFIX + '"' not in html
            or "<iframe" in html.lower()
            or 'id="siteNavigationPanel"' in html):
        raise DownloadPageError("Download engine HTML changed; refusing an incomplete interface")

    start = html.index(_HEADER)
    end = html.find("</header>", start)
    if end == -1:
        raise DownloadPageError("Download engine header was not found")
    end += len("</header>")
    navigation = _NAVIGATION.read_text(encoding="utf-8")
    html = html[:start] + navigation + "\n" + _ACTIONS + html[end:]
    html = html.replace("<title>yt-dlp music server</title>",
                        "<title>Downloads · Music workspace</title>", 1)
    html = html.replace(_HEAD_END, _ASSETS + "\n" + _HEAD_END, 1)
    html = html.replace(_BODY_END, _SCRIPTS + "\n" + _BODY_END, 1)
    return html


async def fetch_native_page(request: Request) -> str:
    """Retrieve a bounded native document from the fixed Docker downloader."""
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
