from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.config_store as store
import app.main as api


def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PLAYLISTS_FILE", tmp_path / "playlists.yml")
    monkeypatch.setattr(store, "STATE_FILE", tmp_path / "playlist-state.yml")
    store.PLAYLISTS_FILE.write_text("""playlists:
  Existing:
    items:
      - rating_key: 1
  Ignored:
    enabled: false
    items:
      - rating_key: 20
""", encoding="utf-8")
    store.merge_text("""playlists:
  Pending:
    items:
      - rating_key: 2
  Ignored:
    enabled: false
    items:
      - rating_key: 21
""", source_filename="new.yml")
    calls = []
    monkeypatch.setattr(api, "connect", lambda: ("fake plex", "fake music"))
    monkeypatch.setattr(api, "hidden_audio_playlist_titles", lambda plex: set())
    def fake_sync(plex, music, name, spec):
        calls.append(name)
        return {"name": name, "status": "synced", "track_count": 1, "matches": []}
    monkeypatch.setattr(api, "sync_playlist", fake_sync)
    loaded = store.load()["playlists"]
    return loaded, calls


def request(name, version_id):
    return api.sync_pending(name, api.PendingSyncBody(version_id=version_id))


def test_bulk_endpoint_only_syncs_pending_and_remembers_revision(tmp_path, monkeypatch):
    playlists, calls = setup(tmp_path, monkeypatch)
    revision = store.fingerprint(playlists)
    assert request("Pending", revision)["status"] == "synced"
    assert store.version_details(store.load()["playlists"])["sync_statuses"]["Pending"] == "synced"
    assert store.pending_names() == ["Ignored"]
    assert request("Pending", revision)["status"] == "skipped"
    assert request("Existing", revision)["status"] == "skipped"
    assert request("Ignored", revision)["status"] == "skipped"
    assert calls == ["Pending"]


def test_bulk_endpoint_rejects_old_loaded_version(tmp_path, monkeypatch):
    playlists, calls = setup(tmp_path, monkeypatch)
    revision = store.fingerprint(playlists)
    store.save_text(store.read_text().replace("rating_key: 2", "rating_key: 999"))
    with pytest.raises(HTTPException) as error:
        request("Pending", revision)
    assert error.value.status_code == 409
    assert calls == []


def test_bulk_endpoint_does_not_modify_hidden_playlists(tmp_path, monkeypatch):
    playlists, calls = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(api, "hidden_audio_playlist_titles", lambda plex: {"Pending"})
    result = request("Pending", store.fingerprint(playlists))
    assert result["status"] == "skipped"
    assert calls == []
    assert "Pending" in store.pending_names()


def test_failed_or_blocked_sync_stays_pending(tmp_path, monkeypatch):
    playlists, calls = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(api, "sync_playlist", lambda *args: {
        "status": "blocked", "message": "One track was not found."
    })
    result = request("Pending", store.fingerprint(playlists))
    assert result["status"] == "blocked"
    assert "Pending" in store.pending_names()
    assert store.version_details(store.load()["playlists"])["sync_statuses"]["Pending"] == "needs_sync"


def test_unknown_playlist_is_not_created(tmp_path, monkeypatch):
    playlists, calls = setup(tmp_path, monkeypatch)
    with pytest.raises(HTTPException) as error:
        request("DoesNotExist", store.fingerprint(playlists))
    assert error.value.status_code == 404
    assert calls == []
