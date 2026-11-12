"""Native downloader integration: real engine template, full-page routing and failures."""
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment

from app import download_page, main

ROOT = Path(__file__).resolve().parents[1]
ENGINE_TEMPLATE = ROOT / "yt-dlp-server" / "templates" / "index.html"


def native_engine_html():
    """Render the real Flask/Jinja template with URL-prefix semantics."""
    def url_for(endpoint, **kwargs):
        if endpoint == "static":
            value = download_page.PREFIX + "/static/" + kwargs["filename"]
            if kwargs.get("v"):
                value += "?v=" + kwargs["v"]
            return value
        routes = {
            "download": "/download",
            "download_chrome_extension": "/chrome-extension.zip",
            "upload_youtube_cookies": "/youtube/cookies/upload",
            "remove_youtube_cookies": "/youtube/cookies/remove",
        }
        return download_page.PREFIX + routes[endpoint]

    template = Environment(autoescape=True).from_string(
        ENGINE_TEMPLATE.read_text(encoding="utf-8")
    )
    return template.render(
        url_for=url_for,
        request=SimpleNamespace(script_root=download_page.PREFIX),
        versions=SimpleNamespace(
            server="test", spotdl="4.5", yt_dlp="test", deno="2.0",
            bgutil="test",
        ),
        jobs=[{"id": "pending_123", "status": "queued"}],
        error="", state_fault=None,
        youtube_cookies_ready=True, youtube_cookie_notice="",
        youtube_cookie_token="synthetic-test-token",
        import_root="/data/music/YT-DLP Imports",
    )


def install_upstream(monkeypatch, respond):
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(respond)

    def factory(*args, **kwargs):
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(download_page.httpx, "AsyncClient", factory)


def test_real_downloader_document_is_one_integrated_workspace():
    html = download_page.compose_native_page(native_engine_html())
    assert html.count("<html") == 1 and html.count("<body") == 1
    assert "<iframe" not in html.lower()
    assert html.count('id="siteNavigationPanel"') == 1
    assert html.count('id="siteNavigationToggle"') == 1
    assert 'src="/static/download-portal.js"' not in html, (
        "The recovery script must never reload a successfully composed native page."
    )
    assert 'id="engine-status"' not in html, (
        "Only the offline fallback may run automatic reconnect checks."
    )
    assert 'href="/downloads-audit?tab=history"' in html
    assert "Find albums missing from Plex" in html
    assert 'id="scan-plex"' not in html
    assert html.count('id="download-form"') == 1
    assert html.count('id="vpn-panel"') == 1
    assert html.count('id="youtube-cookie-panel"') == 1
    for marker in (
        'id="jobs"', 'id="history-results"', 'id="history-query"',
        'id="retry-all-failed"', 'id="vpn-upload"',
        'id="vpn-direct-button"', 'id="youtube-cookie-upload-form"',
        'id="youtube-cookie-remove-button"',
        'href="/playlists"',
        'href="/downloads-audit"', 'href="/static/download-native.css"',
        'href="/static/site-navigation.css"',
        'src="/static/site-navigation.js"',
        '/downloads/engine/static/vpn-manager.js',
        '/downloads/engine/static/youtube-cookies.js',
        'action="/downloads/engine/download"',
        'action="/downloads/engine/youtube/cookies/upload"',
        'action="/downloads/engine/youtube/cookies/remove"',
        'content="/downloads/engine"',
        'const initialJobs = ',
        'const downloaderFetch = ',
    ):
        assert marker in html, marker
    assert 'href="/downloads" data-nav="downloads"' in html
    assert 'id="workspaceTopbar"' in html
    assert html.count('id="signal-theme-toggle"') == 1


def test_native_download_route_uses_engine_html_directly(monkeypatch):
    received = []
    original = native_engine_html()

    def respond(request):
        received.append(request)
        assert request.url.path == "/"
        assert request.url.params["job"] == "pending_123"
        assert request.headers["x-forwarded-prefix"] == "/downloads/engine"
        assert request.headers["accept"] == "text/html"
        return httpx.Response(200, text=original, headers={
            "content-type": "text/html; charset=utf-8",
        })

    install_upstream(monkeypatch, respond)
    with TestClient(main.app) as client:
        page = client.get("/downloads?job=pending_123")
    assert page.status_code == 200
    assert 'id="download-form"' in page.text
    assert 'id="siteNavigationPanel"' in page.text
    assert 'id="engine-status"' not in page.text
    assert 'src="/static/download-portal.js"' not in page.text
    assert 'href="/downloads-audit"' in page.text
    assert 'id="vpn-panel"' in page.text
    assert "<iframe" not in page.text.lower()
    assert len(received) == 1


def test_engine_browser_home_redirects_to_native_page():
    with TestClient(main.app) as client:
        for url in ("/downloads/engine/", "/downloads/engine"):
            response = client.get(url + "?job=pending_123", follow_redirects=False)
            assert response.status_code == 307
            assert response.headers["location"] == "/downloads?job=pending_123"


def test_changed_engine_markup_fails_without_partial_interface():
    original = native_engine_html()
    for modified in (
        original.replace('id="vpn-panel"', 'id="missing-vpn"'),
        original.replace('content="/downloads/engine"', 'content=""'),
        original.replace('id="download-form"', 'id="missing-form"'),
        original + "<iframe src='nested'></iframe>",
    ):
        with pytest.raises(download_page.DownloadPageError):
            download_page.compose_native_page(modified)


def test_bad_engine_html_shows_native_offline_page(monkeypatch):
    def respond(request):
        return httpx.Response(200, text="<html><body>incomplete</body></html>",
                              headers={"content-type": "text/html"})
    install_upstream(monkeypatch, respond)
    with TestClient(main.app) as client:
        response = client.get("/downloads")
    assert response.status_code == 200
    assert "Downloader unavailable" in response.text
    assert 'id="siteNavigationPanel"' in response.text
    assert "<iframe" not in response.text.lower()


def test_engine_timeout_does_not_remove_workflow_navigation(monkeypatch):
    def respond(request):
        raise httpx.ConnectError("connection refused", request=request)
    install_upstream(monkeypatch, respond)
    with TestClient(main.app) as client:
        page = client.get("/downloads")
    assert page.status_code == 200
    assert 'href="/playlists"' in page.text
    assert "automatically and reconnects" in page.text
    assert "<iframe" not in page.text.lower()
