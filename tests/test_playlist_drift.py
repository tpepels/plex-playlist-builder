"""External Plex modifications are reported without mutating live playlists."""
from types import SimpleNamespace as NS
from app import playlist_drift as drift


class Playlist:
    def __init__(self,title,ids,key=55,smart=False):
        self.title=title;self.ratingKey=key;self.smart=smart
        self.radio=False;self.ids=list(ids);self.modified=False
    def items(self):
        return [NS(ratingKey=int(i)) for i in self.ids]


class Plex:
    def __init__(self,items):self._items=items
    def playlists(self,playlistType="audio"):
        return self._items


def setup(monkeypatch,tmp_path,items):
    monkeypatch.setattr(drift,"FILE",tmp_path/"live.json")
    definitions={"Jazz":{"items":[{"rating_key":"1"},{"rating_key":"2"}]},
                 "Missing":{"items":[{"rating_key":"3"}]}}
    monkeypatch.setattr(drift,"load",lambda:{"playlists":definitions})
    monkeypatch.setattr(drift,"connect",lambda:(Plex(items),None))
    monkeypatch.setattr(drift,"resolve_playlist",lambda plex,music,spec:(
        [NS(ratingKey=int(i["rating_key"])) for i in spec["items"]],
        [NS(status="matched") for _ in spec["items"]]))
    return definitions


def test_read_only_detects_reordering_missing_and_preserves_plex_ids(tmp_path,monkeypatch):
    original=Playlist("Jazz",["2","1"])
    setup(monkeypatch,tmp_path,[original])
    checked=drift.refresh()
    assert checked["counts"]=={"drift":1,"missing":1}
    assert checked["playlists"]["Jazz"]["reordered"] is True
    assert checked["playlists"]["Jazz"]["playlist_id"]=="55"
    assert checked["playlists"]["Missing"]["state"]=="missing"
    assert original.ids==["2","1"] and not original.modified
    assert drift.snapshot()["playlists"]["Jazz"]["state"]=="drift"
    original.ids=["1","2"]
    assert drift.refresh()["playlists"]["Jazz"]["state"]=="in_sync"


def test_ambiguous_name_and_smart_playlists_are_not_modified(tmp_path,monkeypatch):
    a=Playlist("Jazz",["1"])
    b=Playlist("JAZZ",["2"])
    setup(monkeypatch,tmp_path,[a,b])
    assert drift.refresh()["playlists"]["Jazz"]["state"]=="ambiguous"
    setup(monkeypatch,tmp_path,[Playlist("Jazz",["1","2"],smart=True)])
    assert drift.refresh()["playlists"]["Jazz"]["state"]=="protected"
