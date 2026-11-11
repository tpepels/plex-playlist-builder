"""Versioned genre-generation API, auditable reviews and exact Plex/YAML publication."""
from __future__ import annotations

import base64
import struct
from types import SimpleNamespace

import pytest
import yaml
from fastapi import HTTPException

from app import main, generation_store as store, generation_publish as pub


def encoded(*values):
    return base64.b64encode(struct.pack("<"+"f"*len(values),*values)).decode("ascii")


def data():
    lib,cache={},{}
    for i in range(1,43):
        key=str(i)
        positive=(i<=4 or i>=10)
        lib[key]={"key":key,"artist":f"Band {i//3}","album":f"Session {i//3}",
                  "album_key":str(i//3),"title":f"Hard bop performance {i}",
                  "genres":["Jazz","Hard Bop"] if i<=4 else (
                      ["Cool Jazz","Jazz"] if i<=9 else []),
                  "year":1960,"duration_ms":400000,"disc":1,"index":i}
        coords=(1.,0.,0.,0.) if positive else (0.,1.,0.,0.)
        cache[key]={"embedding":encoded(*coords),"clap_embedding":encoded(*coords),
                    "energy":.41+(i%6)*.04,"tempo":100+i%9,
                    "other_features":{"relaxed":.4,"happy":.6}}
    return lib,cache


class FakePlex:
    def __init__(self):
        self.playlists_live=[]
        self.created=0
    def playlists(self,playlistType):
        assert playlistType=="audio"
        return self.playlists_live[:]
    def fetchItem(self,key):
        return SimpleNamespace(TYPE="track",ratingKey=key)
    def createPlaylist(self,title,items):
        self.created+=1
        playlist=SimpleNamespace(
            ratingKey=909,title=title,
            _items_source=[SimpleNamespace(ratingKey=i.ratingKey) for i in items])
        playlist.items=lambda:playlist._items_source[:]
        playlist.delete=lambda:self.playlists_live.remove(playlist)
        self.playlists_live.append(playlist)
        return playlist


@pytest.fixture
def state(tmp_path,monkeypatch):
    monkeypatch.setattr(store,"FILE",tmp_path/"drafts.sqlite")
    plex=FakePlex()
    lib,cache=data()
    config={"playlists":{"Existing":{"items":[{"rating_key":"1"}]}}}
    def save(raw):
        content=yaml.safe_load(raw)
        config.clear();config.update(content)
    monkeypatch.setattr(pub,"load",lambda:config)
    monkeypatch.setattr(pub,"read_text",lambda:yaml.safe_dump(config,sort_keys=False))
    monkeypatch.setattr(pub,"save_text",save)
    monkeypatch.setattr(pub,"mark_synced",lambda *_:None)
    monkeypatch.setattr(main,"_generation_context",lambda:(plex,lib))
    monkeypatch.setattr(main,"cached_features",lambda:(cache,{
        "available":len(cache),"cached":len(cache),"pending":0,
        "last_sync":"2026-11-11T12:00:00Z","manifest_version":"test"}))
    monkeypatch.setattr(main,"connect",lambda:(plex,SimpleNamespace()))
    monkeypatch.setattr(main,"get_library",lambda _music:lib)
    monkeypatch.setattr(main,"analysis_status",lambda:{"active":None})
    return plex,lib,cache,config


def request():
    return SimpleNamespace(headers={"origin":"http://localhost:5051"},
                           base_url="http://localhost:5051/")


def body():
    return main.GenerationCreateBody(
        prompt="Hard bop jazz with balanced variety and smooth transitions",
        name="Thirty Hard Bop Recordings",
        count=30,genre="jazz",subgenre="hard bop",
        identity={"variation":"balanced","flow":"smooth"},
    )


def test_readiness_and_reference_search_are_grounded_in_real_ids(state):
    _,lib,cache,_=state
    report=main.generation_readiness()
    assert report["plex_tracks"]==42
    assert report["matched_track_ids"]==42
    assert report["ready"] is True
    genres=main.generation_genre_profiles(genre="jazz",subgenre="hard bop")
    assert genres["profiles"][0]["verified_metadata_count"]==4
    assert genres["profiles"][0]["available"] is True
    search=main.generation_reference_search(q="Band 1",limit=10)
    assert search["tracks"]
    assert all(row["id"] in lib for row in search["tracks"])
    assert all(row["album_track_ids"] for row in search["tracks"])


def test_exact_thirty_review_replace_and_publish_uses_one_existing_pipeline(state):
    plex,lib,cache,config=state
    created=main.create_generation(body(),request())
    identifier=created["id"]
    selected=created["data"]["keys"]
    assert len(selected)==30 and len(set(selected))==30
    assert created["data"]["selection_policy_version"]==2
    assert created["data"]["source_revisions"]["library"]
    assert created["data"]["source_revisions"]["audio"]
    assert created["data"]["genre_profile"]["subgenre"]=="hard bop"
    assert len(created["data"]["genre_evidence"])==30
    assert not plex.created

    target=selected[0]
    alternatives=main.generation_candidates(identifier,target=target,q="",limit=30)
    assert alternatives["revision"]==1
    candidate=next(x for x in alternatives["candidates"] if x["id"] not in selected)
    assert candidate["genre_evidence"]["compatible"]

    # A verdict without the required playback is rejected.
    with pytest.raises(HTTPException,match="15 seconds"):
        main.generation_review(identifier,main.GenerationReviewBody(
            revision=1,candidate_id=candidate["id"],target_id=target,
            verdict="reject",issue="genre",listened=True,
            candidate_played_seconds=4,original_played_seconds=15),request())

    reviewed=main.generation_review(identifier,main.GenerationReviewBody(
        revision=1,candidate_id=candidate["id"],target_id=target,
        verdict="reject",issue="genre",note="Does not belong here",listened=True,
        candidate_played_seconds=16,original_played_seconds=16),request())
    assert reviewed["revision"]==2
    assert reviewed["data"]["reviews"][candidate["id"]]["verdict"]=="reject"
    assert not plex.created
    assert candidate["id"] not in {
        x["id"] for x in main.generation_candidates(identifier,target=target,q="",limit=30)["candidates"]
    }

    alternative=next(x for x in main.generation_candidates(
        identifier,target=target,q="",limit=30)["candidates"] if x["id"]!=candidate["id"])
    edited=main.edit_generation(identifier,main.GenerationEditBody(
        revision=2,operations=[{
            "type":"replace","track_id":target,"candidate_id":alternative["id"]
        }]),request())
    assert edited["revision"]==3
    assert len(edited["data"]["diagnostics"]["transitions"])==29
    assert alternative["id"] in edited["data"]["genre_evidence"]

    preview=main.generation_plan(main.GenerationPlanBody(
        draft_id=identifier,revision=3,name="Thirty Hard Bop Recordings"))
    assert preview["tracks"]==30
    assert preview["subgenre"]=="hard bop"
    assert len(preview["genre_evidence"])==30
    with pytest.raises(HTTPException):
        main.generation_publish(main.GenerationPublishBody(
            draft_id=identifier,revision=3,name=preview["name"],
            plan_id=preview["plan_id"],approved=False),request())
    assert not plex.created
    published=main.generation_publish(main.GenerationPublishBody(
        draft_id=identifier,revision=3,name=preview["name"],
        plan_id=preview["plan_id"],approved=True),request())
    assert published["status"]=="published"
    assert plex.created==1
    expected=edited["data"]["keys"]
    assert [str(x.ratingKey) for x in plex.playlists_live[0].items()]==expected
    assert [str(x["rating_key"]) for x in config["playlists"][preview["name"]]["items"]]==expected
    assert "Existing" in config["playlists"]
    with pytest.raises(HTTPException):
        main.generation_publish(main.GenerationPublishBody(
            draft_id=identifier,revision=3,name=preview["name"],
            plan_id=preview["plan_id"],approved=True),request())
    assert plex.created==1


def test_source_staleness_blocks_publication_until_explicit_regeneration(state):
    plex,lib,cache,_=state
    created=main.create_generation(body(),request())
    identifier=created["id"]
    cache["3"]["energy"]=.99
    with pytest.raises(HTTPException,match="AudioMuse evidence changed"):
        main.generation_plan(main.GenerationPlanBody(
            draft_id=identifier,revision=1,name="New Bop"))
    updated=main.regenerate_generation(identifier,main.GenerationRegenerateBody(
        revision=1),request())
    assert updated["revision"]==2
    assert updated["data"]["source_revisions"]["audio"]!=created["data"]["source_revisions"]["audio"]
    preview=main.generation_plan(main.GenerationPlanBody(
        draft_id=identifier,revision=2,name="New Bop"))
    assert preview["tracks"]==30


def test_exact_genre_count_cannot_be_bypassed_by_manual_remove(state):
    plex,lib,cache,_=state
    created=main.create_generation(body(),request())
    identifier=created["id"]
    target=created["data"]["keys"][0]
    edited=main.edit_generation(identifier,main.GenerationEditBody(
        revision=1,operations=[{"type":"remove","track_id":target}]),request())
    assert len(edited["data"]["keys"])==29
    with pytest.raises(HTTPException,match="exactly 30"):
        main.generation_plan(main.GenerationPlanBody(
            draft_id=identifier,revision=2,name="Incomplete Bop"))
    assert not plex.created


def test_genre_rejection_persists_across_new_drafts_without_cross_genre_leak(state):
    plex,lib,cache,_=state
    original=main.create_generation(body(),request())
    rejected=original["data"]["keys"][0]
    reviewed=main.generation_review(original["id"],main.GenerationReviewBody(
        revision=1,candidate_id=rejected,verdict="reject",issue="genre",
        listened=True,candidate_played_seconds=17),request())
    assert reviewed["revision"]==2
    next_body=body().model_copy(update={"name":"Another Hard Bop"})
    second=main.create_generation(next_body,request())
    assert rejected not in second["data"]["keys"]
    assert second["data"]["selection"]["prior_genre_reviews"]>=1
    assert len(second["data"]["keys"])==30
    # No Plex mutation occurs in genre feedback or new draft ranking.
    assert not plex.created


def test_contextual_energy_rejection_does_not_poison_future_genre_drafts(state):
    plex,lib,cache,_=state
    first=main.create_generation(body(),request())
    target=first["data"]["keys"][0]
    main.generation_review(first["id"],main.GenerationReviewBody(
        revision=1,candidate_id=target,verdict="reject",issue="energy",
        listened=True,candidate_played_seconds=16),request())
    second=main.create_generation(body().model_copy(
        update={"name":"Fresh Energy"}),request())
    assert target in second["data"]["keys"]
    assert not plex.created
