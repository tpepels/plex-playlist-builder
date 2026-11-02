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


def test_plex_ids_from_tsv_text():
    tsv = "artist\trating_key\nA\t101\nB\t102\nA\t101\n"
    assert module.plex_ids_from_tsv(tsv) == {"101", "102"}


def test_manifest_only_skips_live_plex_export(monkeypatch, capsys):
    def fake_manifest(url, **kwargs):
        assert kwargs["progress"] is not None
        return {"tracks": [{"id": "12", "fp": "abc"}],
                "provider": "plex", "total": 1, "pages": 1}

    monkeypatch.setattr(module, "get_manifest", fake_manifest)
    monkeypatch.setattr(module, "plex_ids_from_manager",
                        lambda *args, **kwargs: pytest.fail("Should skip Plex export"))
    assert module.main(["--manifest-only"]) == 0
    captured = capsys.readouterr()
    assert '"manifest_tracks_fetched": 1' in captured.out
    assert '"plex_library_tracks"' not in captured.out
    assert "Skipping Plex ID comparison" in captured.err


def test_manifest_progress_reports_each_page(monkeypatch):
    import json

    pages = iter([
        {"tracks": [{"id": "12", "fp": "a"}], "total_tracks": 2,
         "provider_type": "plex", "has_more": True, "next_page": 2},
        {"tracks": [{"id": "13", "fp": "b"}], "total_tracks": 2,
         "provider_type": "plex", "has_more": False, "next_page": None},
    ])

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, *_args):
            return json.dumps(self.payload).encode()

    monkeypatch.setattr(module, "urlopen", lambda *args, **kwargs: Response(next(pages)))
    updates = []
    result = module.get_manifest("http://localhost:8098", all_pages=True,
                                 progress=updates.append)
    assert [x["id"] for x in result["tracks"]] == ["12", "13"]
    assert result["pages"] == 2
    assert any("Requesting AudioMuse manifest page 2" in x for x in updates)
    assert any("2 total fetched" in x for x in updates)


def test_plex_export_progress(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, *_args):
            return b"rating_key\tartist\n123\tArtist\n"

    monkeypatch.setattr(module, "urlopen", lambda *args, **kwargs: Response())
    updates = []
    assert module.plex_ids_from_manager("http://localhost:5051",
                                        progress=updates.append) == {"123"}
    assert "full-library TSV" in updates[0]
    assert "completed: 1 track IDs" in updates[-1]


def test_token_file_read_from_private_default_location(tmp_path, monkeypatch):
    monkeypatch.delenv("AUDIOMUSE_API_TOKEN", raising=False)
    monkeypatch.delenv("AUDIOMUSE_API_TOKEN_FILE", raising=False)
    monkeypatch.setattr(module.Path, "home", lambda: tmp_path)
    secret = tmp_path / ".config" / "plex-playlist-manager" / "audiomuse-api-token"
    secret.parent.mkdir(parents=True)
    secret.write_text("test-token\n", encoding="utf-8")
    secret.chmod(0o600)
    assert module.load_api_token() == "test-token"


def test_environment_token_takes_priority_over_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIOMUSE_API_TOKEN", "from-environment")
    monkeypatch.setenv("AUDIOMUSE_API_TOKEN_FILE", str(tmp_path / "missing"))
    assert module.load_api_token() == "from-environment"


def test_explicit_token_file_and_permissions(tmp_path, monkeypatch):
    monkeypatch.delenv("AUDIOMUSE_API_TOKEN", raising=False)
    secret = tmp_path / "private-token"
    secret.write_text("from-file", encoding="utf-8")
    secret.chmod(0o600)
    monkeypatch.setenv("AUDIOMUSE_API_TOKEN_FILE", str(secret))
    assert module.load_api_token() == "from-file"
    secret.chmod(0o644)
    with pytest.raises(RuntimeError, match="permissions are too open"):
        module.load_api_token()


def test_missing_explicit_token_file_is_reported(tmp_path, monkeypatch):
    monkeypatch.delenv("AUDIOMUSE_API_TOKEN", raising=False)
    monkeypatch.setenv("AUDIOMUSE_API_TOKEN_FILE", str(tmp_path / "missing"))
    with pytest.raises(RuntimeError, match="does not exist"):
        module.load_api_token()
