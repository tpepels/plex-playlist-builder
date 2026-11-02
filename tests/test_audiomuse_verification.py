"""Read-only Plex lookups must use bounded samples, never full library scans."""
from types import SimpleNamespace

import pytest

from plexapi.exceptions import NotFound

from app.audiomuse_verification import verify_plex_track_keys


class FakePlex:
    def __init__(self):
        self.requested = []

    def fetchItem(self, key):
        self.requested.append(key)
        if key == 3:
            raise NotFound("Missing")
        if key == 4:
            return SimpleNamespace(TYPE="album", title="Not a track")
        return SimpleNamespace(TYPE="track", title="Take Five",
                               grandparentTitle="Dave Brubeck",
                               parentTitle="Time Out")


def test_matching_and_missing_and_type_check():
    plex = FakePlex()
    data = verify_plex_track_keys(plex, ["1", "3", "4", "not-a-key"])
    assert plex.requested == [1, 3, 4]
    assert data["matched"] == 1
    assert [x["status"] for x in data["results"]] == [
        "matched", "missing", "not_track", "invalid"
    ]
    assert data["results"][0]["title"] == "Take Five"


def test_bounded_sample_only():
    plex = FakePlex()
    for ids in [[], [str(i) for i in range(26)]]:
        with pytest.raises(ValueError, match="between 1 and 25"):
            verify_plex_track_keys(plex, ids)
    assert plex.requested == []
