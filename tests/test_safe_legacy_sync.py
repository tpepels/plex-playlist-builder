"""Every legacy playlist writer must preserve existing Plex identity and content."""
from types import SimpleNamespace as NS

import pytest

from app import plex_client


class Playlist:
    def __init__(self, title, keys, id=33):
        self.title=title
        self.ratingKey=id
        self.smart=False
        self.radio=False
        self.deleted=False
        self.cover="existing artwork"
        self.keys=list(keys)
    def items(self):
        return [NS(ratingKey=int(x)) for x in self.keys]
    def addItems(self, tracks):
        self.keys.extend(str(x.ratingKey) for x in tracks)
    def removeItems(self, track):
        self.keys.remove(str(track.ratingKey))
    def moveItem(self, track, after=None):
        key=str(track.ratingKey)
        self.keys.remove(key)
        self.keys.insert(self.keys.index(str(after.ratingKey))+1 if after else 0,key)
    def delete(self):
        self.deleted=True
        raise AssertionError("Existing playlist must never be deleted")


class Plex:
    def __init__(self, playlists=None):
        self.playlists_data=list(playlists or [])
        self.created=0
        self.fail_creation=False
    def playlists(self,playlistType="audio"):
        assert playlistType=="audio"
        return list(self.playlists_data)
    def createPlaylist(self,title,items):
        self.created+=1
        if self.fail_creation:
            raise RuntimeError("Plex create unavailable")
        entry=Playlist(title,[str(x.ratingKey) for x in items],77)
        entry.delete=lambda:self.playlists_data.remove(entry)
        self.playlists_data.append(entry)
        return entry
    def fetchItem(self,ratingKey):
        return NS(TYPE="track",ratingKey=ratingKey)


def install(monkeypatch,keys):
    tracks=[NS(TYPE="track",ratingKey=int(k)) for k in keys]
    monkeypatch.setattr(plex_client,"resolve_playlist",lambda plex,music,spec:
        (tracks,[NS(status="matched",dict=lambda:{"status":"matched"})]))
    monkeypatch.setattr(plex_client,"restore_after_sync",lambda plex,p:{"status":"none"})


def test_updates_all_original_items_in_place_preserving_cover_and_id(monkeypatch):
    install(monkeypatch,["3","1","4"])
    original=Playlist("Jazz",["1","2","3"])
    server=Plex([original])
    result=plex_client.sync_playlist(server,None,"Jazz",{"strict":True})
    assert result["status"]=="synced" and result["changed"] is True
    assert result["playlist_id"]=="33"
    assert original.keys==["3","1","4"]
    assert original.cover=="existing artwork" and not original.deleted
    assert server.playlists_data == [original] and server.created==0


def test_repeated_identical_sync_is_idempotent(monkeypatch):
    install(monkeypatch,["1","2"])
    original=Playlist("Jazz",["1","2"])
    server=Plex([original])
    result=plex_client.sync_playlist(server,None,"Jazz",{"strict":True})
    assert result["status"]=="synced" and result["changed"] is False
    assert original.keys==["1","2"] and server.created==0


def test_plex_failure_rolls_back_original_playlist_and_does_not_delete(monkeypatch):
    install(monkeypatch,["3","1","4"])
    original=Playlist("Jazz",["1","2","3"])
    server=Plex([original])
    move=original.moveItem
    failed={"once":False}
    def move_then_fail(*args,**kwargs):
        if not failed["once"]:
            failed["once"]=True
            raise RuntimeError("Plex move failed")
        return move(*args,**kwargs)
    original.moveItem=move_then_fail
    with pytest.raises(RuntimeError,match="original playlist order was restored"):
        plex_client.sync_playlist(server,None,"Jazz",{"strict":True})
    assert original.keys==["1","2","3"]
    assert not original.deleted and server.created==0


def test_ambiguous_and_smart_existing_playlists_are_never_replaced(monkeypatch):
    install(monkeypatch,["3"])
    same=Playlist("Jazz",["1"])
    second=Playlist("JAZZ",["2"])
    server=Plex([same,second])
    assert plex_client.sync_playlist(server,None,"Jazz",{})["status"]=="blocked"
    assert same.keys==["1"] and second.keys==["2"]
    assert server.created==0
    server=Plex([same])
    same.smart=True
    assert plex_client.sync_playlist(server,None,"Jazz",{})["status"]=="blocked"


def test_failed_new_creation_never_touches_another_playlist(monkeypatch):
    install(monkeypatch,["1"])
    prior=Playlist("Other",["2"])
    server=Plex([prior]);server.fail_creation=True
    with pytest.raises(RuntimeError,match="existing playlists are intact"):
        plex_client.sync_playlist(server,None,"New Playlist",{})
    assert server.playlists_data==[prior]
    assert prior.keys==["2"] and not prior.deleted
