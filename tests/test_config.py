from app.config_store import parse_text


def test_valid_album_and_track():
    data = parse_text('''
playlists:
  Test:
    strict: true
    items:
      - artist: Nick Drake
        album: Pink Moon
      - artist: Bob Dylan
        track: Moonshiner
''')
    assert "Test" in data["playlists"]


def test_invalid_item():
    try:
        parse_text('''
playlists:
  Test:
    items:
      - album: Pink Moon
''')
    except ValueError as exc:
        assert "artist+album" in str(exc)
    else:
        raise AssertionError("Expected invalid config")


def test_valid_rating_key_only_item():
    data = parse_text('''
playlists:
  Test:
    items:
      - rating_key: 12345
''')
    assert data["playlists"]["Test"]["items"][0]["rating_key"] == 12345


def test_merge_marks_changed_playlists_pending_in_import_order(tmp_path, monkeypatch):
    import app.config_store as store

    playlists_file = tmp_path / "playlists.yml"
    state_file = tmp_path / "playlist-state.yml"
    monkeypatch.setattr(store, "PLAYLISTS_FILE", playlists_file)
    monkeypatch.setattr(store, "STATE_FILE", state_file)

    playlists_file.write_text(
        """
playlists:
  Alpha:
    items:
      - rating_key: 1
  Beta:
    items:
      - rating_key: 2
""".lstrip(),
        encoding="utf-8",
    )

    store.merge_text(
        """
playlists:
  Beta:
    items:
      - rating_key: 22
  Gamma:
    items:
      - rating_key: 3
  Alpha:
    items:
      - rating_key: 1
""".lstrip()
    )

    assert store.pending_names() == ["Beta", "Gamma"]

    store.mark_synced("Beta")
    assert store.pending_names() == ["Gamma"]
