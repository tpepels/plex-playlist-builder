"""Fast static UI regressions for the Signal Console redesign."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "templates/index.html").read_text()
STYLE = (ROOT / "static/signal-console.css").read_text()
SCRIPT = (ROOT / "static/signal-console.js").read_text()


def test_downloader_keeps_core_controls_and_adds_theme():
    assert 'id="download-form"' in HTML
    assert 'id="url-input"' in HTML
    assert 'id="jobs"' in HTML
    assert 'id="history-results"' in HTML
    assert 'id="vpn-panel"' in HTML
    assert 'id="youtube-cookie-panel"' in HTML
    assert 'id="youtube-cookie-upload-button"' in HTML
    assert 'id="signal-theme-toggle"' in HTML
    assert "signal-console.css" in HTML
    assert "signal-console.js" in HTML


def test_svg_animation_has_a_still_variant_and_accessibility_support():
    animated = (ROOT / "static/signal-mark.svg").read_text()
    still = (ROOT / "static/signal-mark-static.svg").read_text()
    assert "<svg" in animated and "animation:" in animated
    assert "animation:" not in still
    assert "prefers-reduced-motion:reduce" in animated
    assert "prefers-reduced-motion:reduce" in STYLE
    assert "signal-mark-static.svg" in SCRIPT
    assert 'aria-pressed' in SCRIPT


def test_download_progress_uses_actual_running_state():
    assert "card.classList.add(job.status)" in HTML
    assert ".job.running .track-progress-fill" in STYLE
    assert "signalProgress" in STYLE
