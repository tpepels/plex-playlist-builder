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


def test_no_yaml_initializes_empty_configuration_without_plex(tmp_path, monkeypatch):
    """Missing or empty YAML must not query Plex or block the application."""
    import app.config_store as store
    import app.main as api

    filename = tmp_path / "playlists.yml"
    monkeypatch.setattr(store, "PLAYLISTS_FILE", filename)
    monkeypatch.setattr(store, "STATE_FILE", tmp_path / "playlist-state.yml")

    def no_plex():
        raise AssertionError("GET /api/config must never access Plex")

    monkeypatch.setattr(api, "connect", no_plex)
    response = api.get_config()
    assert response["text"] == "playlists: {}\n"
    assert response["playlists"] == []
    assert response["definitions"] == {}
    assert response["version"]["counts"]["total"] == 0
    assert filename.read_text(encoding="utf-8") == "playlists: {}\n"

    # Existing imported YAML is never replaced by initialization.
    filename.write_text("playlists:\n  Existing:\n    items:\n      - rating_key: 123\n")
    saved = api.get_config()
    assert saved["playlists"] == ["Existing"]
    assert filename.read_text().count("rating_key: 123") == 1


def test_yaml_configuration_available_when_plex_connection_fails(tmp_path, monkeypatch):
    import app.config_store as store
    import app.main as api

    filename = tmp_path / "playlists.yml"
    filename.write_text("playlists:\n  Jazz:\n    items:\n      - rating_key: 42\n")
    monkeypatch.setattr(store, "PLAYLISTS_FILE", filename)
    monkeypatch.setattr(store, "STATE_FILE", tmp_path / "playlist-state.yml")
    monkeypatch.setattr(api, "connect", lambda: (_ for _ in ()).throw(
        ConnectionError("Plex is down")
    ))
    cfg = api.get_config()
    assert cfg["playlists"] == ["Jazz"]
    assert cfg["version"]["counts"]["total"] == 1
    assert cfg["text"] == filename.read_text()
    # Live Plex health still reflects the outage; do not fabricate connectivity.
    from fastapi import Response
    response = Response()
    health = api.health(response)
    assert health["ok"] is False
    assert response.status_code == 503
