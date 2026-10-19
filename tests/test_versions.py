from __future__ import annotations

import yaml

import app.config_store as store


ORIGINAL = """playlists:
  Alpha:
    strict: true
    items:
      - rating_key: 101
  Beta:
    strict: true
    items:
      - rating_key: 202
"""

INCOMING = """playlists:
  Beta:
    strict: true
    items:
      - rating_key: 222
  Gamma:
    strict: true
    items:
      - rating_key: 303
"""


def setup_store(tmp_path, monkeypatch):
    file = tmp_path / "playlists.yml"
    state = tmp_path / "playlist-state.yml"
    monkeypatch.setattr(store, "PLAYLISTS_FILE", file)
    monkeypatch.setattr(store, "STATE_FILE", state)
    file.write_text(ORIGINAL, encoding="utf-8")
    return file, state


def test_revision_ignores_yaml_formatting_and_mapping_order():
    a = {"First": {"strict": True, "items": [{"rating_key": 1}]},
         "Second": {"strict": True, "items": [{"rating_key": 2}]}}
    b = {"Second": {"items": [{"rating_key": 2}], "strict": True},
         "First": {"items": [{"rating_key": 1}], "strict": True}}
    assert store.fingerprint(a) == store.fingerprint(b)
    assert store.fingerprint(a) != store.fingerprint({"First": {"items": [{"rating_key": 2}]}})


def test_inspect_import_shows_changes_without_writing(tmp_path, monkeypatch):
    config_file, state_file = setup_store(tmp_path, monkeypatch)
    compared = store.inspect_import(INCOMING)
    assert compared["new"] == ["Gamma"]
    assert compared["updated"] == ["Beta"]
    assert compared["unchanged"] == []
    assert compared["retained"] == ["Alpha"]
    assert compared["already_loaded"] is False
    assert compared["result_count"] == 3
    assert compared["current_id"] != compared["result_id"]
    assert config_file.read_text(encoding="utf-8") == ORIGINAL
    assert not state_file.exists()


def test_identical_and_subset_packs_are_already_loaded(tmp_path, monkeypatch):
    setup_store(tmp_path, monkeypatch)
    same = store.inspect_import(ORIGINAL)
    assert same["already_loaded"] is True
    assert same["current_id"] == same["result_id"]
    subset = store.inspect_import("""playlists:
  Alpha:
    items:
      - rating_key: 101
    strict: true
""")
    assert subset["already_loaded"] is True
    assert subset["retained"] == ["Beta"]


def test_import_records_filename_revision_and_pending(tmp_path, monkeypatch):
    _, state_file = setup_store(tmp_path, monkeypatch)
    result = store.merge_text(INCOMING, source_filename="exports/plex-playlists-2026-10-19.yml")
    assert list(result["playlists"]) == ["Alpha", "Beta", "Gamma"]
    assert store.pending_names() == ["Beta", "Gamma"]
    status = store.version_details(result["playlists"])
    imp = status["last_import"]
    assert imp["filename"] == "plex-playlists-2026-10-19.yml"
    assert imp["matches_loaded"] is True
    assert imp["source_id"] == store.fingerprint(store.parse_text(INCOMING)["playlists"])
    assert imp["new_count"] == 1
    assert imp["updated_count"] == 1
    assert status["counts"] == {"total": 3, "needs_sync": 2, "synced": 0, "unverified": 1}
    assert yaml.safe_load(state_file.read_text())["last_import"]["loaded_id"] == status["id"]


def test_synced_revision_changes_and_reversion_are_tracked(tmp_path, monkeypatch):
    setup_store(tmp_path, monkeypatch)
    store.merge_text(INCOMING, "new.yml")
    playlists = store.load()["playlists"]
    store.mark_synced("Beta", playlists["Beta"])
    statuses = store.version_details(playlists)["sync_statuses"]
    assert statuses["Beta"] == "synced"
    assert statuses["Gamma"] == "needs_sync"

    altered = store.read_text().replace("rating_key: 222", "rating_key: 223")
    store.save_text(altered)
    statuses = store.version_details(store.load()["playlists"])["sync_statuses"]
    assert statuses["Beta"] == "needs_sync"
    assert store.version_details(store.load()["playlists"])["last_import"]["matches_loaded"] is False

    restored = store.read_text().replace("rating_key: 223", "rating_key: 222")
    store.save_text(restored)
    statuses = store.version_details(store.load()["playlists"])["sync_statuses"]
    assert statuses["Beta"] == "synced"
    assert store.version_details(store.load()["playlists"])["last_import"]["matches_loaded"] is True


def test_legacy_state_is_not_reported_as_verified_sync(tmp_path, monkeypatch):
    _, state_file = setup_store(tmp_path, monkeypatch)
    state_file.write_text("pending:\n  - Beta\n", encoding="utf-8")
    statuses = store.version_details(store.load()["playlists"])["sync_statuses"]
    assert statuses == {"Alpha": "unverified", "Beta": "needs_sync"}
    store.mark_synced("Beta")
    statuses = store.version_details(store.load()["playlists"])["sync_statuses"]
    assert statuses["Beta"] == "unverified"
