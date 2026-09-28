from app.plex_client import _best_album, _best_track


class O:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_album_prefers_artist_and_title():
    candidates = [
        O(title="Pink Moon", parentTitle="Someone Else"),
        O(title="Pink Moon", parentTitle="Nick Drake"),
    ]
    result = _best_album(candidates, "Nick Drake", "Pink Moon")
    assert result.parentTitle == "Nick Drake"


def test_album_normalizes_punctuation_and_accents():
    candidates = [O(title="Música callada", parentTitle="Federico Mompou")]
    result = _best_album(candidates, "Federico Mompou", "Musica Callada")
    assert result is not None


def test_track_uses_album_when_given():
    candidates = [
        O(title="Moonshiner", grandparentTitle="Bob Dylan", parentTitle="Other"),
        O(title="Moonshiner", grandparentTitle="Bob Dylan", parentTitle="Bootleg Series"),
    ]
    result = _best_track(candidates, "Bob Dylan", "Moonshiner", "Bootleg Series")
    assert result.parentTitle == "Bootleg Series"
