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
