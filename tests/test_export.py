from app.plex_client import export_audio_playlists


class O:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Playlist(O):
    def items(self):
        return self._items


class Plex:
    def __init__(self, playlists):
        self._playlists = playlists

    def playlists(self, playlistType=None):
        assert playlistType == "audio"
        return self._playlists


def test_export_audio_playlists_preserves_track_order_and_metadata():
    second = O(
        ratingKey=202,
        grandparentTitle="Artist B",
        parentTitle="Album B",
        title="Second",
    )
    first = O(
        ratingKey=101,
        grandparentTitle="Artist A",
        parentTitle="Album A",
        title="First",
    )
    plex = Plex([
        Playlist(title="Zed", summary="", _items=[second, first]),
        Playlist(title="Alpha", summary="A saved Plex playlist", _items=[first]),
    ])

    exported = export_audio_playlists(plex)

    assert list(exported) == ["Alpha", "Zed"]
    assert exported["Alpha"]["description"] == "A saved Plex playlist"
    assert exported["Alpha"]["strict"] is True
    assert exported["Zed"]["items"] == [
        {
            "rating_key": "202",
            "artist": "Artist B",
            "album": "Album B",
            "track": "Second",
        },
        {
            "rating_key": "101",
            "artist": "Artist A",
            "album": "Album A",
            "track": "First",
        },
    ]


def test_export_audio_playlists_keeps_rating_key_when_metadata_is_sparse():
    track = O(ratingKey=303, grandparentTitle="", parentTitle="", title="")
    plex = Plex([Playlist(title="Sparse", summary="", _items=[track])])

    exported = export_audio_playlists(plex)

    assert exported["Sparse"]["items"] == [{"rating_key": "303"}]


def test_export_audio_playlists_skips_smart_radio_and_known_utility_playlists():
    track = O(
        ratingKey=404,
        grandparentTitle="Artist",
        parentTitle="Album",
        title="Track",
    )
    plex = Plex([
        Playlist(title="All Music", summary="", smart=False, radio=False, _items=[track]),
        Playlist(title="Recently Added", summary="", smart=True, radio=False, _items=[track]),
        Playlist(title="Radio", summary="", smart=False, radio=True, _items=[track]),
        Playlist(title="Manual", summary="", smart=False, radio=False, _items=[track]),
    ])

    exported = export_audio_playlists(plex)

    assert list(exported) == ["Manual"]
