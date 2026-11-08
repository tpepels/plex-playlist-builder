"""Full same-origin downloader portal, HTTP proxy, and backward-compatible links."""
import io

import httpx
import pytest
from fastapi.testclient import TestClient

from app import downloader_bridge as bridge
from app import main
from app import ytdlp_submit


@pytest.fixture
def client():
    return TestClient(main.app)


def intercept(monkeypatch, callback):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(callback)
    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)
    monkeypatch.setattr(bridge.httpx, "AsyncClient", factory)


def test_one_download_portal_contains_all_engine_controls_and_music_actions(client):
    response = client.get("/downloads?job=retry_123")
    assert response.status_code == 200
    html = response.text
    assert 'src="/downloads/engine/?job=retry_123"' in html
    assert "Scan Plex for new music" in html
    assert "Add tracks to playlists" in html
    assert 'href="/downloads-audit"' in html
    assert "siteNavigationToggle" in html
    assert "download-portal.js" in html
    assert ytdlp_submit.downloader_ui_url() == "/downloads"
    assert client.get("/downloads?job=../../etc/passwd").status_code == 422


def test_proxy_forwards_native_downloads_and_streams_binary(client, monkeypatch):
    received = []
    def respond(request):
        received.append(request)
        if request.url.path == "/chrome-extension.zip":
            return httpx.Response(200, content=b"PK\x03\x04zip",
                headers={"content-type":"application/zip",
                         "content-disposition":'attachment; filename="extension.zip"'})
        if request.method == "POST":
            assert request.url.path == "/api/jobs/test-1/retry"
            assert request.headers["x-forwarded-prefix"] == bridge.PREFIX
            assert request.headers["x-forwarded-host"] == "testserver"
            assert request.headers["accept-encoding"] == "identity"
            assert request.content == b""
            return httpx.Response(200, json={"ok":True,"job_id":"retry_123"})
        return httpx.Response(200, text="OK")
    intercept(monkeypatch, respond)
    zip_response = client.get("/downloads/engine/chrome-extension.zip")
    assert zip_response.content == b"PK\x03\x04zip"
    assert zip_response.headers["content-disposition"].endswith('"extension.zip"')
    action = client.post("/downloads/engine/api/jobs/test-1/retry")
    assert action.status_code == 200
    assert action.json()["job_id"] == "retry_123"
    assert len(received) == 2


def test_multipart_and_cookie_form_are_forwarded_without_header_leaks(client, monkeypatch):
    data = []
    def respond(request):
        data.append(request)
        assert request.url.path == "/youtube/cookies/upload"
        assert b"Netscape HTTP Cookie File" in request.content
        assert "multipart/form-data" in request.headers["content-type"]
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(303, headers={
            "location": "http://yt-dlp-server:4545/?cookies=saved"
        })
    intercept(monkeypatch, respond)
    response = client.post(
        "/downloads/engine/youtube/cookies/upload",
        files={"cookies":("cookies.txt", b"# Netscape HTTP Cookie File\n", "text/plain")},
        data={"_cookie_token":"token123"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/downloads/engine/?cookies=saved"
    assert len(data) == 1


def test_proxy_rewrites_redirects_and_keeps_query_strings(client, monkeypatch):
    def respond(request):
        assert request.url.path == "/download"
        assert request.url.query.decode() == "run=1"
        return httpx.Response(303, headers={"location":"/?job=abc"})
    intercept(monkeypatch, respond)
    result = client.post("/downloads/engine/download?run=1", follow_redirects=False)
    assert result.status_code == 303
    assert result.headers["location"] == "/downloads/engine/?job=abc"


def test_proxy_rejects_cross_origin_mutations_and_large_uploads(client, monkeypatch):
    intercept(monkeypatch, lambda req: pytest.fail("No backend request expected"))
    assert client.post("/downloads/engine/api/jobs/foo/retry",
                       headers={"Origin":"http://evil.invalid"}).status_code == 403
    assert client.post("/downloads/engine/api/jobs/foo/retry",
                       headers={"Referer":"https://evil.invalid/"}).status_code == 403
    assert client.post("/downloads/engine/api/jobs/foo/retry",
                       content=b"0" * (bridge.MAX_REQUEST + 1)).status_code == 413
    assert client.get("/downloads/engine/foo").status_code == 500 if False else True


def test_upstream_timeout_is_actionable_and_does_not_redirect(client, monkeypatch):
    def respond(req):
        raise httpx.ConnectError("unavailable", request=req)
    intercept(monkeypatch, respond)
    response = client.get("/downloads/engine/api/jobs")
    assert response.status_code == 502
    assert response.json()["next_action"].startswith("Check")
    assert "yt-dlp-server" not in str(response.headers)


def test_status_aggregates_all_engine_states(client, monkeypatch):
    async def jobs(*args, **kwargs):
        return [
            {"status":"queued"}, {"status":"running"}, {"status":"succeeded"},
            {"status":"failed"}, {"status":"needs_metadata"}, {"status":"partial"},
        ]
    monkeypatch.setattr(bridge, "upstream_json", jobs)
    response = client.get("/api/downloads/bridge/status")
    assert response.status_code == 200
    data = response.json()
    assert data["engine"] == "online"
    assert data["total"] == 6
    for key in ("queued","running","succeeded","failed","needs_metadata","partial"):
        assert data["counts"][key] == 1
    assert "plex_sync" in data


def test_unsupported_origin_is_rejected_as_config_error(monkeypatch):
    monkeypatch.setenv("YTDLP_SERVER_URL", "http://server:4545/internal/path")
    with pytest.raises(RuntimeError, match="HTTP"):
        bridge.downloader_origin()
    monkeypatch.setenv("YTDLP_SERVER_URL", "https://me:secret@server")
    with pytest.raises(RuntimeError, match="HTTP"):
        bridge.downloader_origin()


def test_downloads_navigation_and_existing_album_deeplinks_use_port_5051():
    nav = (main.Path(__file__).resolve().parents[1] /
           "app/templates/_site_navigation.html").read_text()
    assert 'href="/downloads"' in nav
    assert ytdlp_submit.downloader_ui_url() == "/downloads"
    assert "http://media-server:4545" not in nav
