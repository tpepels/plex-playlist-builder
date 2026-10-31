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
    assert "aria-pressed" in script


def test_all_pages_load_theme_and_brand():
    for filename in ("index.html", "album_download.html", "disk_audit.html",
                     "download_audit.html"):
        html = (TEMPLATES / filename).read_text()
        assert "/static/signal-console.css" in html, filename
        assert "/static/signal-console.js" in html, filename
        assert "signal-theme-toggle" in html, filename
        assert "signal-mark.svg" in html, filename


def test_existing_playlist_actions_remain_available():
    html = (TEMPLATES / "index.html").read_text()
    for control in ("importPack", "cards", "syncPending", "viewHealth",
                    "viewDuplicates", "viewIsc", "viewDiscover", "packFile"):
        assert f'id="{control}"' in html
    main = (ROOT / "app/main.py").read_text()
    assert 'StaticFiles(directory=str(Path(__file__).parent / "static"))' in main
