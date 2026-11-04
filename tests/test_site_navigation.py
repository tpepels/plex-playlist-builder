"""Every HTML screen renders the same shared secondary navigation.

The dashboard has its own matching hamburger menu because it hosts six
internal views and the actual cover-ZIP file picker.
"""
from pathlib import Path
import re

from jinja2 import Environment, FileSystemLoader

TEMPLATES=Path(__file__).resolve().parents[1]/"app"/"templates"
PAGES=(
    "playlist_generator.html",
    "playlist_intelligence.html",
    "musical_coherence.html",
    "disk_audit.html",
    "download_audit.html",
    "album_download.html",
)
VIEW_KEYS=("playlists","health","duplicates","newmusic","isc","discover")
DESTINATIONS=(
    "/playlist-generator",
    "/playlist-intelligence",
    "/musical-coherence",
    "/downloads-audit",
    "/api/library-albums.tsv",
    "/api/library-export.tsv",
    "/api/playlists-export.yml",
)


def test_all_secondary_templates_render_the_same_navigation():
    env=Environment(loader=FileSystemLoader(str(TEMPLATES)),autoescape=True)
    menus=[]
    for page in PAGES:
        rendered=env.get_template(page).render(
            artist="Bill Evans",album="Waltz for Debby",
            token="test",search_url="https://music.youtube.com/search?q=test",
        )
        assert rendered.count('id="siteNavigationToggle"')==1,page
        assert rendered.count('id="siteNavigationPanel"')==1,page
        assert rendered.count('id="siteNavigationBackdrop"')==1,page
        assert rendered.count('id="siteNavigationClose"')==1,page
        assert rendered.count('href="/static/site-navigation.css"')==1,page
        assert rendered.count('src="/static/site-navigation.js"')==1,page
        assert 'role="dialog" aria-modal="true"' in rendered,page
        assert 'aria-expanded="false"' in rendered,page
        for view in VIEW_KEYS:
            assert f'href="/?view={view}"' in rendered,page
        for destination in DESTINATIONS:
            assert f'href="{destination}"' in rendered,page
        assert 'id="siteNavRecentForm"' in rendered,page
        assert 'name="mode"' in rendered and 'name="limit"' in rendered,page
        assert rendered.index('id="siteNavigationToggle"') < rendered.index(
            'id="siteNavigationPanel"'),page
        # Check the actual menu subtree, ignoring page-specific headers.
        contents=rendered.split('id="siteNavigationPanel"',1)[1].split("</aside>",1)[0]
        menus.append(contents)
    assert all(menu==menus[0] for menu in menus[1:])


def test_dashboard_has_matching_destinations_and_deep_link_handler():
    html=(TEMPLATES/"index.html").read_text(encoding="utf-8")
    assert html.count('id="navigationPanel"')==1
    assert html.count('id="navigationToggle"')==1
    for destination in DESTINATIONS:
        assert f'href="{destination}"' in html
    for view in VIEW_KEYS:
        assert f'data-view="{view}"' in html
    assert 'new URLSearchParams(window.location.search).get("view")' in html
    assert 'window.history.replaceState' in html


def test_all_html_route_templates_are_covered():
    import app.main as main
    route_paths={
        route.path for route in main.app.routes if getattr(route,"path",None)
    }
    for target in ("/","/playlist-generator","/playlist-intelligence",
                   "/musical-coherence","/downloads-audit",
                   "/open-youtube-album"):
        assert target in route_paths
    for page in PAGES:
        source=(TEMPLATES/page).read_text(encoding="utf-8")
        assert '{% include "_site_navigation.html" %}' in source,page
