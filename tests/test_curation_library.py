"""Persistent Plex catalogue: complete scans, reuse and atomic failure behavior."""
import json
from types import SimpleNamespace

import pytest

from app import curation_library as idx


def track(key):
    return SimpleNamespace(ratingKey=key,parentRatingKey=12,grandparentTitle="Artist",
        parentTitle="Album",title="Song "+str(key),parentIndex=1,index=int(key))


class Music:
    def __init__(self):
        self.calls=0
        self.fail=False
    def searchTracks(self, **kwargs):
        self.calls+=1
        assert kwargs["container_size"]==200
        if self.fail:
            raise RuntimeError("Plex disconnected")
        return [track(1),track(2),track(3)]


def test_index_complete_once_then_reused_persistently(tmp_path,monkeypatch):
    monkeypatch.setattr(idx,"FILE",tmp_path/"library.json")
    monkeypatch.setattr(idx,"_CACHED",{})
    server=Music()
    a=idx.get_library(server)
    assert len(a)==3
    assert server.calls==1
    assert idx.get_library(server)==a
    assert server.calls==1
    assert idx.metadata()["tracks"]==3
    monkeypatch.setattr(idx,"_CACHED",{})
    assert idx.get_library(server)==a
    assert server.calls==1
    assert json.loads(idx.FILE.read_text())["tracks"]["1"]["title"]=="Song 1"


def test_failed_refresh_preserves_last_complete_catalogue(tmp_path,monkeypatch):
    monkeypatch.setattr(idx,"FILE",tmp_path/"library.json")
    monkeypatch.setattr(idx,"_CACHED",{})
    server=Music()
    old=idx.get_library(server)
    server.fail=True
    with pytest.raises(RuntimeError,match="disconnected"):
        idx.get_library(server,refresh=True)
    assert idx.get_library(server)==old
    assert idx.metadata()["tracks"]==3


def test_invalid_partial_catalogue_is_rejected(tmp_path,monkeypatch):
    monkeypatch.setattr(idx,"FILE",tmp_path/"library.json")
    monkeypatch.setattr(idx,"_CACHED",{})
    server=Music()
    monkeypatch.setattr(server,"searchTracks",lambda **kwargs:[])
    with pytest.raises(RuntimeError,match="empty"):
        idx.get_library(server,refresh=True)
    assert idx.metadata()["tracks"]==0



def test_album_genres_and_release_year_enrich_tracks_without_n_plus_one_queries(
    tmp_path,monkeypatch
):
    monkeypatch.setattr(idx,"FILE",tmp_path/"library.json")
    monkeypatch.setattr(idx,"_CACHED",{})
    base=Music()
    class WithAlbums:
        def __init__(self):
            self.track_calls=0
            self.album_calls=0
        def searchTracks(self,**kwargs):
            self.track_calls+=1
            result=base.searchTracks(**kwargs)
            for track in result:
                track.parentRatingKey=12
                track.parentYear=None
                track.genres=[]
            return result
        def searchAlbums(self,**kwargs):
            self.album_calls+=1
            assert kwargs["container_size"]==200
            return [SimpleNamespace(ratingKey=12,year=1961,
                                    genres=[SimpleNamespace(tag="Jazz"),
                                            SimpleNamespace(tag="Cool Jazz")])]
    plex=WithAlbums()
    catalogue=idx.get_library(plex,refresh=True)
    assert all(v["year"]==1961 for v in catalogue.values())
    assert all(v["genres"]==["Jazz","Cool Jazz"] for v in catalogue.values())
    assert plex.track_calls==1 and plex.album_calls==1
    monkeypatch.setattr(idx,"_CACHED",{})
    persisted=idx.get_library(plex)
    assert persisted["1"]["year"]==1961
    assert plex.track_calls==1


def test_stale_snapshot_is_returned_without_blocking_for_plex_network(tmp_path,monkeypatch):
    import time
    monkeypatch.setattr(idx,"FILE",tmp_path/"library.json")
    monkeypatch.setattr(idx,"_CACHED",{})
    server=Music()
    original=idx.get_library(server)
    envelope=json.loads(idx.FILE.read_text())
    envelope["updated"]=time.time()-86400
    idx.FILE.write_text(json.dumps(envelope))
    monkeypatch.setattr(idx,"_CACHED",{})
    server.fail=True
    assert idx.get_library(server)==original
    assert server.calls==1
    with pytest.raises(RuntimeError,match="Plex disconnected"):
        idx.get_library(server,refresh=True)


def test_reads_continue_during_long_background_refresh(tmp_path,monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(idx,"FILE",tmp_path/"library.json")
    monkeypatch.setattr(idx,"_CACHED",{})
    idx.get_library(Music())
    started=threading.Event()
    release=threading.Event()
    class SlowMusic(Music):
        def searchTracks(self,**kwargs):
            started.set()
            assert release.wait(4), "Background test stalled"
            return super().searchTracks(**kwargs)
    server=SlowMusic()
    work=threading.Thread(target=lambda:idx.get_library(server,refresh=True))
    work.start()
    try:
        assert started.wait(2)
        # Old implementation held the cache lock while Plex was scanning.
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(idx.get_library,server)
            assert len(future.result(timeout=1))==3
    finally:
        release.set()
        work.join(timeout=4)
