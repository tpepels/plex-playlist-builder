"""Home is a read-only, integrated entry point, with legacy URLs preserved."""
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "app" / "templates" / "home.html"
JS = ROOT / "app" / "static" / "home.js"
CSS = ROOT / "app" / "static" / "home.css"


def test_home_is_application_root_and_has_full_navigation():
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        html = response.text
        assert "Add music. Build playlists." in html
        assert html.count('id="siteNavigationPanel"') == 1
        assert html.count('id="siteNavigationToggle"') == 1
        assert 'id="workspaceTopbar"' in html
        assert 'href="/" data-nav="home"' in html
        assert 'href="/downloads"' in html
        assert 'href="/playlists"' in html
        assert 'href="/playlists?work=create"' in html
        assert 'href="/playlists?work=improve"' in html
        assert 'href="/playlists?work=analysis"' in html
        assert 'href="/downloads-audit"' in html
        assert 'src="/static/home.js"' in html
        assert 'href="/static/home.css"' in html
        assert 'src="/static/site-navigation.js"' in html
        assert "media-server:4545" not in html
        assert 'id="home-playlist-file"' in html
        assert 'id="home-download-form"' in html
        assert 'action="/downloads/engine/download"' in html
        assert 'src="/static/home-intake.js"' in html


def test_full_dashboard_stays_at_canonical_and_legacy_bookmarks():
    with TestClient(app) as client:
        for url in ("/playlists", "/playlists?view=health", "/?view=health",
                    "/?view=discover", "/?view=newmusic"):
            page = client.get(url)
            assert page.status_code == 200, url
            assert 'id="viewPlaylists"' in page.text
            assert 'id="viewDiscover"' in page.text
            assert 'id="siteNavigationPanel"' in page.text
            assert 'id="importCoverZip"' in page.text
            assert 'id="importPack"' in page.text
        assert 'id="viewPlaylists"' not in client.get("/").text
        assert 'id="viewPlaylists"' not in client.get("/?view=unrecognized").text


def test_landing_page_never_causes_writes_or_backend_calls_on_server_render(monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "connect", lambda: (_ for _ in ()).throw(
        AssertionError("Home should render without touching Plex")))
    monkeypatch.setattr(main, "load", lambda: (_ for _ in ()).throw(
        AssertionError("Home should render without reading playlist data")))
    with TestClient(main.app) as client:
        assert client.get("/").status_code == 200


def test_home_status_is_read_only_and_has_failure_states():
    markup = HTML.read_text()
    code = JS.read_text()
    for target in ("plex-value", "playlist-count", "download-count",
                   "sync-count", "attention-list"):
        assert f'id="{target}"' in markup
    for endpoint in ("/api/health", "/api/config/summary", "/api/downloads/bridge/status"):
        assert endpoint in code
    assert "Promise.allSettled" in code
    assert "workspace:changed" in code
    assert "response.ok" in code
    assert "Offline" in code
    assert "textContent" in code
    assert "innerHTML" not in code
    assert "document.hidden" in code
    assert "@media(max-width:520px)" in CSS.read_text()


def test_playlist_revision_status_is_read_only_and_does_not_export_yaml(monkeypatch):
    from app import main
    monkeypatch.setattr(main, "load", lambda: {"playlists":{
        "Updated playlist":{"items":[{"rating_key":"123"}]}
    }})
    monkeypatch.setattr(main, "version_details", lambda defs: {
        "id":"latest-version", "last_import":None,
        "sync_statuses":{"Updated playlist":"needs_sync"},
        "counts":{"total":1,"needs_sync":1,"synced":0,"unverified":0}
    })
    status = main.config_status()
    assert set(status) == {"version","live_verification"}
    assert status["version"]["id"] == "latest-version"
    assert status["version"]["counts"]["needs_sync"] == 1
    assert "definitions" not in status
    assert "text" not in status


def test_home_has_background_status_and_pending_deep_link():
    markup = HTML.read_text(encoding="utf-8")
    script = JS.read_text(encoding="utf-8")
    assert 'href="/playlists?pending=1"' in markup
    assert 'id="maintenance-status"' in markup
    assert '"/api/workspace/maintenance"' in script
    assert 'link:"/playlists?pending=1"' in script
