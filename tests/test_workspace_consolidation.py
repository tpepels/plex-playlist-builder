"""One playlist workspace URL covers all existing engines and preserves legacy links."""
from fastapi.testclient import TestClient
from app.main import app


def test_workspace_sections_share_one_navigation_and_style():
    targets = {
        "library": "id=\"viewPlaylists\"",
        "create": "id=\"createForm\"",
        "improve": "id=\"validationResults\"",
        "analysis": "id=\"autoMaintenance\"",
    }
    with TestClient(app) as client:
        for work, marker in targets.items():
            url = "/playlists" if work == "library" else f"/playlists?work={work}"
            response = client.get(url)
            assert response.status_code == 200, url
            html = response.text
            assert marker in html, url
            assert html.count('id="siteNavigationPanel"') == 1
            assert html.count('aria-label="Playlist workflows"') == 1
            assert html.count('href="/static/site-navigation.css"') == 1
            assert html.count('src="/static/site-navigation.js"') == 1
            assert 'href="/playlists?work=create"' in html
            assert 'href="/playlists?work=improve"' in html
            assert 'href="/playlists?work=analysis"' in html


def test_secondary_library_views_remain_in_same_workspace():
    with TestClient(app) as client:
        for view in ("health", "duplicates", "newmusic", "isc", "discover"):
            html = client.get("/playlists?view=" + view).text
            assert 'id="viewPlaylists"' in html
            assert 'id="siteNavigationPanel"' in html
            assert 'aria-label="Playlist workflows"' in html


def test_unknown_workspace_is_rejected_without_fallback():
    with TestClient(app) as client:
        response = client.get("/playlists?work=nonexistent")
    assert response.status_code == 404


def test_legacy_generator_curator_and_audio_analysis_stay_functional():
    with TestClient(app) as client:
        for route, marker in (
            ("/playlist-generator", 'id="createForm"'),
            ("/playlist-intelligence", 'id="playlist"'),
            ("/musical-coherence", 'id="reports"'),
        ):
            response = client.get(route)
            assert response.status_code == 200, route
            assert marker in response.text
            assert 'aria-label="Playlist workflows"' in response.text
