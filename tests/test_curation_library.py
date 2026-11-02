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
