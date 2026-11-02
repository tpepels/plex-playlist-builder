"""Playlist Intelligence: complete resolution, search, staged editing and Plex persistence."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import struct
from types import SimpleNamespace

import pytest

from app import playlist_curator as curator
from app import curation_apply as changes
from app.config_store import fingerprint


def t(key, artist="Artist", album="Album", title=None, track=1):
    return SimpleNamespace(ratingKey=int(key), parentRatingKey=1,
        grandparentTitle=artist, parentTitle=album, title=title or f"Song {key}",
        parentIndex=1, index=track, TYPE="track")


def lib():
    return curator.catalogue([
        t(1, "Coltrane", "Blue Train", "Blue Train", 1),
        t(2, "Coltrane", "Blue Train", "Moment's Notice", 2),
        t(3, "Evans", "Explorations", "Israel", 1),
        t(4, "Evans", "Explorations", "Haunted Heart", 2),
        t(5, "Miles", "Kind of Blue", "So What", 1),
        t(6, "Miles", "Kind of Blue", "Freddie Freeloader", 2),
        t(7, "Getz", "Live", "Stella", 1),
        t(8, "Hall", "Live", "Stella", 1),
        t(9, "Getz", "Live", "Stella", 2),
    ])


def b64(*v):
    return base64.b64encode(struct.pack("<"+"f"*len(v),*v)).decode()


def cached():
    return {str(i):{"embedding":b64(1.,0.,0.,0.) if i!=8 else b64(0.,1.,0.,0.),
                    "clap_embedding":b64(1.,1.,0.,0.),
                    "energy": .3 if i!=8 else .9, "tempo":105,
                    "other_features":{"relaxed":.7,"happy":.5}} for i in range(1,10)}


def test_resolves_all_three_yaml_item_types_with_album_order():
    library=lib()
    spec={"items":[{"artist":"Coltrane","album":"Blue Train"},
        {"artist":"Evans","track":"Israel","album":"Explorations"},
        {"rating_key":5}]}
    keys,problems=curator.resolve_items(spec,library)
    assert keys==["1","2","3","5"]
    assert problems==[]


def test_disambiguates_tracks_and_rejects_duplicates_and_missing():
    library=lib()
    spec={"items":[{"artist":"Getz","track":"Stella"},
                   {"rating_key":1},{"rating_key":1},{"rating_key":999}]}
    keys,problems=curator.resolve_items(spec,library)
    assert keys==["1"]
    assert {p["reason"].split()[0] for p in problems}=={"Ambiguous","Duplicate","Missing"}


def test_multidimensional_review_and_no_score_without_coverage():
    library=lib()
    keys=[str(x) for x in range(1,9)]
    data=curator.assess("Jazz",{"description":"Exploratory jazz","items":[{"rating_key":key} for key in keys]},library,cached())
    assert data["profile"]["confidence"]=="sufficient"
    assert data["profile"]["tracks"]==8
    assert data["profile"]["artists"]==5
    assert data["reviews"][0]["track_id"]=="8"
    sparse=curator.assess("Jazz",{"items":[{"rating_key":key} for key in keys]},library,{})
    assert sparse["reviews"]==[]
    assert sparse["profile"]["confidence"]=="limited"


def test_candidates_search_full_catalogue_without_hallucinated_scores():
    library=lib()
    many=curator.candidates(["1","2","3"], library,cached(),target="1")
    assert all(entry["id"] not in ("1","2","3") for entry in many)
    assert many[0]["match"] is not None
    other=curator.candidates(["1","2","3"],library,{},query="Freddie")
    assert [entry["id"] for entry in other]==["6"]
    assert other[0]["match"] is None


def test_suggestion_requires_present_target():
    with pytest.raises(curator.CurationConflict):
        curator.candidates(["1"],lib(),{},target="999")


def test_staged_add_remove_replace_move_and_preview():
    library=lib()
    ops=[{"type":"replace","track_id":"2","candidate_id":"4"},
         {"type":"remove","track_id":"3"},
         {"type":"add","candidate_id":"5"},
         {"type":"move","track_id":"5","after_id":None}]
    new=curator.compute_operations(["1","2","3"],ops,library)
    assert new==["5","1","4"]
    plan=changes.change_plan(["1","2","3"],ops,library,name="Jazz",version="v",snapshot="s")
    assert plan["after"]==new
    assert [t["key"] for t in plan["added"]]==["5","4"]


@pytest.mark.parametrize("ops",[
    [{"type":"add","candidate_id":"1"}],
    [{"type":"remove","track_id":"999"}],
    [{"type":"replace","track_id":"1","candidate_id":"999"}],
    [{"type":"move","track_id":"1","after_id":"999"}],
    [{"type":"unknown"}],
])
def test_invalid_changes_are_rejected(ops):
    with pytest.raises(curator.CurationConflict):
        curator.compute_operations(["1","2"],ops,lib())


class Playlist:
    def __init__(self, keys):
        self._live=list(keys)
        self.ratingKey=444
        self.title="Jazz"
        self.smart=False
        self.radio=False
        self.calls=[]

    def items(self):
        return [t(key) for key in self._live]

    def addItems(self, items):
        self.calls.append("add")
        self._live.extend(str(item.ratingKey) for item in items)

    def removeItems(self, item):
        self.calls.append("remove")
        self._live.remove(str(item.ratingKey))

    def moveItem(self, item, after=None):
        self.calls.append("move")
        self._live.remove(str(item.ratingKey))
        pos=self._live.index(str(after.ratingKey))+1 if after is not None else 0
        self._live.insert(pos,str(item.ratingKey))


class Plex:
    def __init__(self, playlist):
        self.playlist=playlist

    def playlists(self, playlistType):
        assert playlistType=="audio"
        return [self.playlist]

    def fetchItem(self, key):
        return t(key)


def setup_apply(monkeypatch):
    stored={"playlists":{"Jazz":{"strict":True, "description":"Quiet jazz",
                                  "items":[{"rating_key":key} for key in ["1","2","3"]]}}}
    monkeypatch.setattr(changes,"load",lambda:stored)
    saved=[]
    monkeypatch.setattr(changes,"save_text",lambda text:saved.append(text))
    synced=[]
    monkeypatch.setattr(changes,"mark_synced",lambda name,spec:synced.append((name,spec)))
    return stored,saved,synced


def test_approved_update_modifies_existing_playlist_and_persists_yaml(monkeypatch):
    stored,saved,synced=setup_apply(monkeypatch)
    live=Playlist(["1","2","3"])
    plex=Plex(live)
    version=fingerprint(stored["playlists"])
    snap=curator.signature(live._live)
    ops=[{"type":"replace","track_id":"2","candidate_id":"5"},
         {"type":"move","track_id":"5","after_id":None}]
    plan=changes.change_plan(live._live,ops,lib(),name="Jazz",version=version,snapshot=snap)
    result=changes.apply_approved(plex,"Jazz",version=version,snapshot=snap,
                   operations=ops,plan_id=plan["plan_id"],library=lib(),approved=True,
                   forbidden={"All Music"})
    assert live._live==["5","1","3"]
    assert result["playlist_id"]=="444"
    assert result["verified"] is True
    assert "5" in saved[0]
    assert "Quiet jazz" in saved[0]
    assert synced[0][0]=="Jazz"
    assert "move" in live.calls


def test_rejects_stale_plex_or_yaml_and_unapproved(monkeypatch):
    stored,saved,synced=setup_apply(monkeypatch)
    live=Playlist(["1","2","3"])
    plex=Plex(live)
    version=fingerprint(stored["playlists"])
    ops=[{"type":"remove","track_id":"1"}]
    for kwargs in ({"approved":False,"snapshot":curator.signature(live._live)},
                   {"approved":True,"snapshot":"old"},
                   {"approved":True,"snapshot":curator.signature(live._live),"version":"stale"}):
        with pytest.raises(curator.CurationConflict):
            changes.apply_approved(plex,"Jazz",version=kwargs.get("version",version),
                snapshot=kwargs["snapshot"],operations=ops,plan_id="bad",
                library=lib(),approved=kwargs["approved"],forbidden=set())
    assert not live.calls
    assert not saved


def test_yaml_failure_rolls_plex_playlist_back(monkeypatch):
    stored,saved,synced=setup_apply(monkeypatch)
    def fail_save(content):
        raise OSError("disk full")
    monkeypatch.setattr(changes,"save_text",fail_save)
    live=Playlist(["1","2","3"])
    plex=Plex(live)
    version=fingerprint(stored["playlists"])
    snap=curator.signature(live._live)
    ops=[{"type":"replace","track_id":"1","candidate_id":"5"}]
    plan=changes.change_plan(live._live,ops,lib(),name="Jazz",version=version,snapshot=snap)
    with pytest.raises(RuntimeError,match="restored"):
        changes.apply_approved(plex,"Jazz",version=version,snapshot=snap,
            operations=ops,plan_id=plan["plan_id"],library=lib(),approved=True,forbidden=set())
    assert live._live==["1","2","3"]


def test_album_yaml_is_not_lost_when_previewing():
    data=curator.assess("Jazz",{"items":[{"artist":"Coltrane","album":"Blue Train"}]},lib(),cached())
    assert data["keys"]==["1","2"]
    assert data["issues"]==[]


def test_system_playlist_is_protected(monkeypatch):
    setup_apply(monkeypatch)
    with pytest.raises(curator.CurationConflict,match="System"):
        changes.apply_approved(Plex(Playlist(["1"])),"All Music",version="v",snapshot="s",
            operations=[{"type":"remove","track_id":"1"}],plan_id="x",
            library=lib(),approved=True,forbidden={"All Music"})
