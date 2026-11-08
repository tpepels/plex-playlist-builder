"""Verify playlist workspace bootstrap against actual HTML, not a permissive mock.

Every top-level DOM lookup must have a real element. A deleted toolbar control
previously threw before either the Plex health check or YAML load could start.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "app/templates/index.html").read_text(encoding="utf-8")


def test_every_inline_dom_id_lookup_exists_in_current_markup():
    html_ids = set(re.findall(r'\\bid="([^"]+)"', TEMPLATE))
    script = TEMPLATE.split("<script>", 1)[1].split("</script>", 1)[0]
    lookups = set(re.findall(r'\\$\\("([^"]+)"\\)', script))
    missing = sorted(lookups - html_ids)
    assert not missing, (
        "Orphaned DOM lookup(s) abort playlist workspace initialization: "
        + ", ".join(missing)
    )


def test_export_form_owned_by_shared_navigation_not_removed_legacy_toolbar():
    nav = (ROOT / "app/templates/_site_navigation.html").read_text(encoding="utf-8")
    navigation_js = (ROOT / "app/static/site-navigation.js").read_text(encoding="utf-8")
    script = TEMPLATE.split("<script>", 1)[1].split("</script>", 1)[0]
    for identifier in ("siteNavRecentMode", "siteNavRecentLimit", "siteNavRecentForm"):
        assert f'id="{identifier}"' in nav
        assert identifier in navigation_js
    assert '$("recentMode")' not in script
    assert '$("recentLimit")' not in script


def test_plex_health_and_yaml_load_remain_independent_at_startup():
    script = TEMPLATE.split("<script>", 1)[1].split("</script>", 1)[0]
    assert 'checkHealth();' in script
    assert 'loadConfig().catch' in script
    assert "No playlist definitions yet." in script
    assert 'url === "/api/config" || url === "/api/health"' in script
