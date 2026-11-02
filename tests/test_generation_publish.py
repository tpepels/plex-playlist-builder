"""Durable drafts and create-only Plex/YAML publication with failure recovery."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from app import generation_store as store
from app import generation_publish as publication
from app.playlist_generation import parse_prompt


def library():
    return {str(i):{"key":str(i),"artist":"Artist "+str(i),
         "album":"Album "+str(i),"album_key":str(i),
         "title":"Song "+str(i),"disc":1,"index":1,
         "duration_ms":300000,"genres":["Jazz"],"year":1960}
         for i in range(1,6)}


def record():
    return {"request":parse_prompt("Live jazz, 1960-1965",count=3),
        "keys":["1","2","3"],
        "selection":{"eligible":5},
        "warnings":[],
        "diagnostics":{"tracks":3,"flags":[]}}


class Playlist:
    def __init__(self,plex,name,ids):
        self.plex=plex
        self.title=name
        self.ratingKey=900
        self._keys=list(ids)
        self.deleted=False
    def items(self):
        return [SimpleNamespace(ratingKey=int(x)) for x in self._keys]
    def delete(self):
        self.deleted=True
        self.plex._playlists.remove(self)


class Plex:
    def __init__(self):
        self._playlists=[]
        self.created=0
        self.fetches=[]
        self.corrupt=False
    def playlists(self,playlistType=None):
        assert playlistType=="audio"
        return list(self._playlists)
    def fetchItem(self,key):
        self.fetches.append(key)
        return SimpleNamespace(TYPE="track",ratingKey=key)
    def createPlaylist(self,title,items):
        self.created+=1
        keys=[str(item.ratingKey) for item in items]
        if self.corrupt:keys.reverse()
        obj=Playlist(self,title,keys)
        self._playlists.append(obj)
        return obj


@pytest.fixture
def setup(monkeypatch,tmp_path):
    monkeypatch.setattr(store,"FILE",tmp_path/"generation.sqlite")
    config={"playlists":{"Existing":{"items":[{"rating_key":"5"}]}}}
    saved=[]
    def write(text):
        next_data=yaml.safe_load(text)
        config.clear()
        config.update(next_data)
        saved.append(text)
    monkeypatch.setattr(publication,"load",lambda:config)
    monkeypatch.setattr(publication,"read_text",
        lambda:yaml.safe_dump(config,sort_keys=False))
    monkeypatch.setattr(publication,"save_text",write)
    synced=[]
    monkeypatch.setattr(publication,"mark_synced",
        lambda name,spec:synced.append((name,spec)))
    return config,saved,synced


def test_draft_survives_database_reopen_and_rejects_stale_edits(setup):
    created=store.create(record())
    assert created["revision"]==1
    assert store.get(created["id"])["data"]["keys"]==["1","2","3"]
    modified=dict(created["data"],keys=["1","3","2"])
    latest=store.revise(created["id"],1,modified)
    assert latest["revision"]==2
    with pytest.raises(store.DraftConflict):
        store.revise(created["id"],1,modified)
    assert store.get(created["id"])["data"]["keys"]==["1","3","2"]


def test_explicit_approval_creates_exactly_one_new_playlist_and_yaml(setup):
    config,saved,synced=setup
    plex=Plex()
    draft=store.create(record())
    plan=publication.prepare(plex,draft,"New Jazz",library())
    assert plan["tracks"]==3
    with pytest.raises(store.DraftConflict,match="approval"):
        publication.publish(plex,draft["id"],1,"New Jazz",
                            plan["plan_id"],False,library())
    assert plex.created==0
    result=publication.publish(plex,draft["id"],1,"New Jazz",
                               plan["plan_id"],True,library())
    assert result["verified"] is True
    assert result["playlist_id"]=="900"
    assert plex.created==1
    assert plex._playlists[0]._keys==["1","2","3"]
    assert "Existing" in config["playlists"]
    created=config["playlists"]["New Jazz"]
    assert created["strict"] is True
    assert created["description"]=="Live jazz, 1960-1965"
    assert [x["rating_key"] for x in created["items"]]==["1","2","3"]
    assert synced[0][0]=="New Jazz"
    assert store.get(draft["id"])["status"]=="published"
    with pytest.raises(store.DraftConflict):
        publication.publish(plex,draft["id"],1,"New Jazz",
                            plan["plan_id"],True,library())
    assert plex.created==1


def test_draft_revision_or_yaml_version_mismatch_blocks_before_write(setup):
    config,saved,synced=setup
    plex=Plex()
    draft=store.create(record())
    plan=publication.prepare(plex,draft,"Fresh title",library())
    changed=dict(draft["data"],keys=["3","1","2"])
    store.revise(draft["id"],1,changed)
    with pytest.raises(store.DraftConflict):
        publication.publish(plex,draft["id"],1,"Fresh title",
                            plan["plan_id"],True,library())
    newdraft=store.create(record())
    plan=publication.prepare(plex,newdraft,"Another title",library())
    config["playlists"]["Other editor"]={"items":[{"rating_key":"1"}]}
    with pytest.raises(store.DraftConflict):
        publication.publish(plex,newdraft["id"],1,"Another title",
                            plan["plan_id"],True,library())
    assert plex.created==0 and not saved


def test_name_collision_is_case_insensitive_and_never_replaces_existing(setup):
    config,saved,synced=setup
    plex=Plex()
    plex._playlists.append(Playlist(plex,"Protected",["5"]))
    draft=store.create(record())
    for title in ("Existing","protected"):
        with pytest.raises(store.DraftConflict,match="already exists"):
            publication.prepare(plex,draft,title,library())
    assert len(plex._playlists)==1
    assert plex._playlists[0]._keys==["5"]


def test_write_failure_deletes_only_new_playlist_and_restores_yaml(setup,monkeypatch):
    config,saved,synced=setup
    plex=Plex()
    prior=Playlist(plex,"Existing",["5"])
    plex._playlists.append(prior)
    draft=store.create(record())
    plan=publication.prepare(plex,draft,"Rollback test",library())
    def fail_save(text):
        raise OSError("Disk full")
    monkeypatch.setattr(publication,"save_text",fail_save)
    with pytest.raises(RuntimeError,match="rolled back"):
        publication.publish(plex,draft["id"],1,"Rollback test",
                            plan["plan_id"],True,library())
    assert plex._playlists==[prior]
    assert not prior.deleted
    assert store.get(draft["id"])["status"]=="draft"


def test_plex_order_mismatch_rolls_back_before_yaml_write(setup):
    config,saved,synced=setup
    plex=Plex()
    plex.corrupt=True
    draft=store.create(record())
    plan=publication.prepare(plex,draft,"Wrong order",library())
    with pytest.raises(RuntimeError,match="rolled back"):
        publication.publish(plex,draft["id"],1,"Wrong order",
                            plan["plan_id"],True,library())
    assert not plex._playlists and not saved


def test_sync_state_failure_reports_repair_not_false_success(setup,monkeypatch):
    config,saved,synced=setup
    monkeypatch.setattr(publication,"mark_synced",
        lambda *a:(_ for _ in ()).throw(OSError("failed state write")))
    plex=Plex()
    draft=store.create(record())
    plan=publication.prepare(plex,draft,"Repair required",library())
    result=publication.publish(plex,draft["id"],1,"Repair required",
                               plan["plan_id"],True,library())
    assert result["sync_state"]=="state_repair_required"
    assert store.get(draft["id"])["status"]=="published"
    assert plex._playlists[0]._keys==["1","2","3"]
