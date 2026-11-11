"""One workspace navigation across every HTML screen and downloader composition."""
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"
PAGES = (
    "playlist_generator.html", "playlist_intelligence.html",
    "musical_coherence.html", "disk_audit.html", "download_audit.html",
    "album_download.html", "download_portal.html", "home.html", "index.html",
)
DESTINATIONS = (
    "/", "/downloads", "/playlists",
    "/playlists?work=analysis", "/downloads-audit",
    "/api/library-albums.tsv", "/api/library-export.tsv",
    "/api/playlists-export.yml",
)


def rendered_page(filename):
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
    return env.get_template(filename).render(
        artist="Bill Evans", album="Waltz for Debby", token="test",
        search_url="https://music.youtube.com/search?q=test",
        downloader_url="/downloads", engine_url="/downloads/engine/",
    )


def test_all_routes_share_one_identical_header_and_theme():
    headers = []
    for name in PAGES:
        html = rendered_page(name)
        assert html.count('id="workspaceTopbar"') == 1, name
        assert html.count('id="siteNavigationPanel"') == 1, name
        assert html.count('id="siteNavigationToggle"') == 1, name
        assert html.count('id="workspaceMore"') == 1, name
        assert html.count('id="signal-theme-toggle"') == 1, name
        assert html.count('href="/static/signal-console.css"') == 1, name
        assert html.count('href="/static/site-navigation.css"') == 1, name
        assert html.count('src="/static/site-navigation.js"') == 1, name
        assert html.count('src="/static/signal-console.js"') == 1, name
        assert html.count('aria-label="Workspace navigation"') == 1, name
        assert 'id="siteNavigationBackdrop"' not in html, name
        assert 'id="siteNavigationClose"' not in html, name
        assert 'class="view-tabs workspace-view-tabs"' not in html, name
        for target in DESTINATIONS:
            assert f'href="{target}"' in html, (name, target)
        assert 'id="siteNavRecentForm"' in html, name
        assert html.count('data-nav="') == 3, name
        start = html.index('<header class="workspace-topbar"')
        end = html.index("</header>", start) + len("</header>")
        headers.append(html[start:end])
    assert all(header == headers[0] for header in headers[1:])


def test_library_sections_have_direct_urls_without_secondary_tab_navigation():
    source = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    assert source.count('{% include "_site_navigation.html" %}') == 1
    assert "showWorkspaceView(requestedView" in source
    assert "view-tabs workspace-view-tabs" not in source
    assert "window.history.replaceState" not in source
    for view in ("playlists", "health", "duplicates", "newmusic", "isc", "discover"):
        assert f'{view}:' in source
    for control in ("importCoverZip", "importPack", "viewHealth",
                    "viewDuplicates", "viewIsc", "viewDiscover"):
        assert f'id="{control}"' in source


def test_all_html_route_templates_are_covered():
    import app.main as main
    route_paths = {route.path for route in main.app.routes if getattr(route, "path", None)}
    for path in ("/", "/playlists", "/downloads", "/playlist-generator",
                 "/playlist-intelligence", "/musical-coherence",
                 "/downloads-audit", "/open-youtube-album"):
        assert path in route_paths
    for page in PAGES:
        assert '{% include "_site_navigation.html" %}' in (
            TEMPLATES / page).read_text(encoding="utf-8")


def test_workflow_actions_live_in_playlist_workspace():
    html = rendered_page("index.html")
    for target in ("/playlists?work=create", "/playlists?work=improve",
                   "/playlists?view=isc", "/playlists?view=health",
                   "/playlists?view=newmusic", "/playlists?view=duplicates"):
        assert f'href="{target}"' in html
    header = rendered_page("home.html")
    assert 'href="/playlists?work=create"' in header
    assert 'href="/playlists?work=improve"' in header
