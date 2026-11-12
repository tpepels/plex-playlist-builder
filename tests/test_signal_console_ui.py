"""Static checks for the playlist manager's shared visual system."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app/static"
TEMPLATES = ROOT / "app/templates"


def test_shared_theme_assets_present_and_respect_reduced_motion():
    animated = (STATIC / "signal-mark.svg").read_text()
    still = (STATIC / "signal-mark-static.svg").read_text()
    style = (STATIC / "signal-console.css").read_text()
    script = (STATIC / "signal-console.js").read_text()
    assert "animation:" in animated
    assert "animation:" not in still
    assert "prefers-reduced-motion:reduce" in animated
    assert "prefers-reduced-motion:reduce" in style
    assert "signal-mark-static.svg" in script
    assert '["cyber", "slate", "terminal"]' in script
    assert "select.addEventListener(\"change\"" in script
    assert "signal-console-theme-v1" in script
    terminal = (STATIC / "site-navigation.css").read_text()
    assert 'html[data-signal-theme="terminal"]' in terminal
    assert "prefers-reduced-motion:reduce" in terminal


def test_all_pages_load_theme_and_brand():
    navigation = (TEMPLATES / "_site_navigation.html").read_text()
    assert navigation.count('id="signal-theme-toggle"') == 1
    assert '<select id="signal-theme-toggle"' in navigation
    for theme in ("cyber","slate","terminal"):
        assert f'<option value="{theme}">' in navigation
    assert "aria-pressed" not in navigation
    assert "signal-mark.svg" in navigation
    for filename in ("index.html", "album_download.html", "disk_audit.html",
                     "download_audit.html", "home.html", "playlist_generator.html",
                     "playlist_intelligence.html", "musical_coherence.html"):
        source = (TEMPLATES / filename).read_text()
        assert 'href="/static/signal-console.css"' in source, filename
        assert 'src="/static/signal-console.js"' in source, filename
        assert source.count('{% include "_site_navigation.html" %}') == 1, filename


def test_existing_playlist_actions_remain_available():
    html = (TEMPLATES / "index.html").read_text()
    for control in ("importPack", "cards", "syncPending", "viewHealth",
                    "viewDuplicates", "viewIsc", "viewDiscover", "packFile"):
        assert f'id="{control}"' in html
    main = (ROOT / "app/main.py").read_text()
    assert 'StaticFiles(directory=str(Path(__file__).parent / "static"))' in main
