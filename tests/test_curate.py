from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import main, config_store


def make_track(key, album_id="album-42"):
    return SimpleNamespace(
        ratingKey=key,
        parentRatingKey=album_id,
        grandparentTitle="Artist",
        parentTitle="Record",
        title=f"Track {key}",
        addedAt=datetime(2026, 10, 26, tzinfo=timezone.utc),
    )


def setup(tmp_path, monkeypatch):
    filename = tmp_path / "playlists.yml"
    state = tmp_path / "playlist-state.yml"
    filename.write_text(
        "playlists:\n  Main:\n    strict: true\n    items:\n      - rating_key: '1'\n  Keep:\n    strict: true\n    items:\n      - rating_key: '3'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_store, "PLAYLISTS_FILE", filename)
    monkeypatch.setattr(config_store, "STATE_FILE", state)
    monkeypatch.setattr(main, "connect", lambda: (object(), object()))
    monkeypatch.setattr(main, "_insights_tracks",
                        lambda music: [make_track(1), make_track(2), make_track(3, "other")])
    return filename, state


def command(playlist="Main", keys=None, album="album-42", version=None):
    definitions = config_store.load()["playlists"]
    return main.CurateBody(
        playlist=playlist,
        rating_keys=keys if keys is not None else ["2"],
        album_key=album,
        version_id=version or config_store.fingerprint(definitions),
    )


def test_explicit_addition_only_updates_target_playlist(tmp_path, monkeypatch):
    file, state = setup(tmp_path, monkeypatch)
    original_other = config_store.load()["playlists"]["Keep"]
    resp = main.curate_tracks(command())
    assert resp == {"added": 1, "status": "needs_sync", "playlist": "Main"}
    updated = config_store.load()["playlists"]
    assert [str(x["rating_key"]) for x in updated["Main"]["items"]] == ["1", "2"]
    assert updated["Keep"] == original_other
    assert config_store.pending_names() == ["Main"]
    # No Plex API playlist mutation: this endpoint only changes YAML.
    assert file.exists() and state.exists()


def test_duplicate_addition_does_not_duplicate_track_or_write(tmp_path, monkeypatch):
    file, state = setup(tmp_path, monkeypatch)
    resp = main.curate_tracks(command(keys=["1"]))
    assert resp["added"] == 0
    assert not state.exists()
    assert len(config_store.load()["playlists"]["Main"]["items"]) == 1


def test_stale_version_protects_saved_yaml(tmp_path, monkeypatch):
    file, _ = setup(tmp_path, monkeypatch)
    before = file.read_text()
    with pytest.raises(HTTPException) as raised:
        main.curate_tracks(command(version="obsolete"))
    assert raised.value.status_code == 409
    assert file.read_text() == before


def test_album_mismatch_rejected_before_mutation(tmp_path, monkeypatch):
    file, _ = setup(tmp_path, monkeypatch)
    before = file.read_text()
    with pytest.raises(HTTPException) as raised:
        main.curate_tracks(command(keys=["3"]))
    assert raised.value.status_code == 422
    assert file.read_text() == before


def test_cannot_add_to_nonexisting_playlist(tmp_path, monkeypatch):
    file, _ = setup(tmp_path, monkeypatch)
    with pytest.raises(HTTPException) as raised:
        main.curate_tracks(command(playlist="Nothing"))
    assert raised.value.status_code == 404


def test_album_level_playlist_reference_prevents_duplicate_track(tmp_path, monkeypatch):
    file, _ = setup(tmp_path, monkeypatch)
    file.write_text(
        "playlists:\n  Main:\n    items:\n      - artist: Artist\n        album: Record\n",
        encoding="utf-8",
    )
    result = main.curate_tracks(command(keys=["1", "2"]))
    assert result["added"] == 0
    assert len(config_store.load()["playlists"]["Main"]["items"]) == 1


def test_one_click_youtube_resolver_redirects_only_after_exact_match(monkeypatch):
    monkeypatch.setattr(main, "lookup_album", lambda artist, album: {
        "status": "found",
        "url": "https://music.youtube.com/playlist?list=OLAK5uy_correct",
    })
    response = main.open_youtube_album("Artist", "Album")
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://music.youtube.com/playlist?")


def test_one_click_youtube_resolver_not_found_is_not_search_redirect(monkeypatch):
    monkeypatch.setattr(main, "lookup_album", lambda artist, album: {
        "status": "not_found",
        "message": "No exact album match.",
    })
    with pytest.raises(HTTPException) as raised:
        main.open_youtube_album("Artist", "Unknown")
    assert raised.value.status_code == 404
