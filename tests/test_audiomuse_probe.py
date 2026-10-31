"""Fast, network-free tests for AudioMuse manifest/Plex matching."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "audiomuse_probe", Path(__file__).parents[1] / "scripts" / "audiomuse_probe.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_plex_ids(tmp_path):
    p = tmp_path / "music.tsv"
    p.write_text("artist\trating_key\nA\t10\nB\t11\nC\t10\n", encoding="utf-8")
    assert module.plex_ids(p) == {"10", "11"}


def test_missing_column(tmp_path):
    p = tmp_path / "music.tsv"
    p.write_text("artist\ttitle\nA\tX\n", encoding="utf-8")
    with pytest.raises(ValueError, match="rating_key"):
        module.plex_ids(p)


def test_compare_exact_ids():
    manifest = {
        "tracks": [{"id": "10", "fp": "abc"}, {"id": "else", "fp": "def"}],
        "provider": "plex", "total": 2, "pages": 1,
    }
    report = module.compare(manifest, {"10", "11"})
    assert report["exact_plex_rating_key_matches"] == 1
    assert report["mapping_validated"]
    assert report["unmatched_sample"] == ["else"]


def test_compare_no_tracks():
    manifest = {"tracks": [], "provider": "plex", "total": 0, "pages": 1}
    report = module.compare(manifest, {"10"})
    assert report["match_percentage_of_fetched"] is None
    assert not report["mapping_validated"]


def test_bearer_auth_header_preferred_over_browser_cookie():
    assert module.auth_headers(token="secret", cookie="session=old") == {
        "Accept": "application/json",
        "Authorization": "Bearer secret",
    }


def test_browser_cookie_fallback():
    assert module.auth_headers(cookie="session=xyz")["Cookie"] == "session=xyz"
