"""End-to-end generator API routes with real selection and mocked Plex writes."""
from __future__ import annotations

from types import SimpleNamespace
import pytest
import yaml
from fastapi import HTTPException

from app import main
from app import generation_store as store
from app import generation_publish as publisher


class Plex:
    def __init__(self):
        self.playlists_live=[]
        self.created=0
    def playlists(self,playlistType):
        assert playlistType=="audio"
        return list(self.playlists_live)
    def fetchItem(self,key):
        return SimpleNamespace(TYPE="track",ratingKey=key)
    def createPlaylist(self,title,items):
        playlist=SimpleNamespace(
            ratingKey=777,title=title,
            _items_source=[SimpleNamespace(ratingKey=i.ratingKey) for i in items]
        )
        def items_func():return list(playlist._items_source)
        playlist.items=items_func
        playlist.delete=lambda:self.playlists_live.remove(playlist)
        self.playlists_live.append(playlist)
        self.created+=1
        return playlist


def ctx():
    data={}
    for i in range(1,20):
        data[str(i)]={
            "key":str(i),"artist":"Artist "+str(i//3),
            "album":"Live Jazz Club "+str(i//3),"album_key":str(i//3),
            "title":"Performance "+str(i),"year":1960,"genres":["Jazz"],
            "duration_ms":600000,"disc":1,"index":i
        }
    return data


@pytest.fixture
def state(monkeypatch,tmp_path):
    monkeypatch.setattr(store,"FILE",tmp_path/"drafts.sqlite")
    plex=Plex()
    lib=ctx()
    config={"playlists":{"Existing":{"items":[{"rating_key":"1"}]}}}
    def save(raw):
        config.clear()
        config.update(yaml.safe_load(raw))
    monkeypatch.setattr(main,"_generation_context",lambda:(plex,lib))
    monkeypatch.setattr(main,"cached_features",lambda:({},{"cached":0}))
    monkeypatch.setattr(publisher,"load",lambda:config)
    monkeypatch.setattr(publisher,"read_text",lambda:yaml.safe_dump(config,sort_keys=False))
    monkeypatch.setattr(publisher,"save_text",save)
    monkeypatch.setattr(publisher,"mark_synced",lambda *a:None)
    return plex,lib,config


def request(origin="http://localhost:5051"):
    return SimpleNamespace(headers={"origin":origin},base_url="http://localhost:5051/")


def new():
    return main.GenerationCreateBody(
        name="The Intimate Club",
        prompt="Intimate live jazz, 1955–1965, long improvisations",count=7
    )


def test_generator_routes_cover_generation_review_edit_suggestions_and_publication(state):
    plex,lib,config=state
    with pytest.raises(HTTPException) as blocked:
        main.create_generation(new(),request("http://evil.invalid"))
    assert blocked.value.status_code==403
    created=main.create_generation(new(),request())
    assert created["data"]["name"]=="The Intimate Club"
    assert len(created["data"]["keys"])==7
    identifier=created["id"]
    assert main.get_generation(identifier)["data"]["keys"]==created["data"]["keys"]
    target=created["data"]["keys"][0]
    results=main.generation_candidates(identifier,target=target,q="",limit=20)
    assert results["revision"]==1
    alternative=next((row["id"] for row in results["candidates"]
                      if row["id"] not in created["data"]["keys"]),None)
    assert alternative is not None
    updated=main.edit_generation(identifier,main.GenerationEditBody(
        revision=1,operations=[{"type":"replace","track_id":target,"candidate_id":alternative}]
    ),request())
    assert updated["revision"]==2
    assert alternative in updated["data"]["keys"] and target not in updated["data"]["keys"]
    assert updated["data"]["diagnostics"]["tracks"]==7
    with pytest.raises(HTTPException) as conflict:
        main.edit_generation(identifier,main.GenerationEditBody(
            revision=1,operations=[{"type":"remove","track_id":updated["data"]["keys"][0]}]
        ),request())
    assert conflict.value.status_code==409
    preview=main.generation_plan(main.GenerationPlanBody(
        draft_id=identifier,revision=2,name="The Intimate Club"))
    assert preview["tracks"]==7
    assert len(preview["track_order"])==7
    with pytest.raises(HTTPException) as rejected:
        main.generation_publish(main.GenerationPublishBody(
            draft_id=identifier,revision=2,name="The Intimate Club",
            plan_id=preview["plan_id"],approved=True),
            request("http://evil.invalid"))
    assert rejected.value.status_code==403
    result=main.generation_publish(main.GenerationPublishBody(
        draft_id=identifier,revision=2,name="The Intimate Club",
        plan_id=preview["plan_id"],approved=True),request())
    assert result["verified"] and result["status"]=="published"
    assert plex.created==1
    assert "The Intimate Club" in config["playlists"]
    assert main.get_generation(identifier)["status"]=="published"
    with pytest.raises(HTTPException) as duplicate:
        main.generation_publish(main.GenerationPublishBody(
            draft_id=identifier,revision=2,name="The Intimate Club",
            plan_id=preview["plan_id"],approved=True),request())
    assert duplicate.value.status_code==409 and plex.created==1


def test_generation_explicit_name_collision_and_unapproved_block(state):
    plex,lib,config=state
    created=main.create_generation(new(),request())
    body=main.GenerationPlanBody(draft_id=created["id"],revision=1,name="Existing")
    with pytest.raises(HTTPException) as collision:
        main.generation_plan(body)
    assert collision.value.status_code==409
    safe=main.generation_plan(main.GenerationPlanBody(
        draft_id=created["id"],revision=1,name="Unique collection"))
    with pytest.raises(HTTPException) as unapproved:
        main.generation_publish(main.GenerationPublishBody(
          draft_id=created["id"],revision=1,name="Unique collection",
          plan_id=safe["plan_id"],approved=False),request())
    assert unapproved.value.status_code==409 and plex.created==0
