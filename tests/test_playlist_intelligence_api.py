"""Route-level integration checks: end-to-end Plex review and approval boundaries."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import main
from app.config_store import fingerprint
from app.playlist_curator import signature


def item(key):
    return SimpleNamespace(ratingKey=int(key), parentRatingKey=1,
        grandparentTitle="Artist", parentTitle="Album", title=f"Song {key}",
        parentIndex=1,index=int(key),TYPE="track")


class MockPlaylist:
    title="Jazz"
    ratingKey=202
    smart=False
    radio=False
    def items(self):
        return [item(key) for key in ("1","2")]


def context():
    library={"1":{"key":"1","artist":"Artist","album":"Album","title":"Song 1","album_key":"1","disc":1,"index":1},
             "2":{"key":"2","artist":"Artist","album":"Album","title":"Song 2","album_key":"1","disc":1,"index":2},
             "3":{"key":"3","artist":"Other","album":"Album","title":"Song 3","album_key":"2","disc":1,"index":1}}
    config={"Jazz":{"description":"mixed jazz","items":[{"rating_key":"1"},{"rating_key":"2"}]}}
    return SimpleNamespace(),None,library,config,MockPlaylist(),["1","2"]


def test_profile_endpoint_reports_live_plex_data(monkeypatch):
    monkeypatch.setattr(main,"_curation_context",lambda name: context())
    monkeypatch.setattr(main,"cached_features",lambda:({},{"cached":0}))
    result=main.curation_playlist("Jazz")
    assert result["keys"]==["1","2"]
    assert result["profile"]["confidence"]=="limited"
    assert result["playlist_id"]=="202"
    assert result["yaml_in_sync"] is True


def test_candidate_endpoint_has_safe_search_and_no_fake_scores(monkeypatch):
    monkeypatch.setattr(main,"_curation_context",lambda name:context())
    monkeypatch.setattr(main,"cached_features",lambda:({},{"cached":0}))
    result=main.get_curation_candidates("Jazz",target="1",q="Song 3",limit=20)
    assert [row["id"] for row in result["candidates"]]==["3"]
    assert result["candidates"][0]["match"] is None


def test_plan_endpoint_rejects_stale_snapshot(monkeypatch):
    monkeypatch.setattr(main,"_curation_context",lambda name:context())
    data=context()[3]
    args=main.CurationPlanBody(name="Jazz",version=fingerprint(data),
        snapshot="stale",operations=[{"type":"add","candidate_id":"3"}])
    with pytest.raises(HTTPException) as exc:
        main.curation_plan(args)
    assert exc.value.status_code==409


def test_apply_requires_same_origin_and_approval(monkeypatch):
    monkeypatch.setattr(main,"_curation_context",lambda name: context())
    calls=[]
    monkeypatch.setattr(main,"apply_approved",lambda *args,**kwargs:calls.append(kwargs) or {"verified":True})
    b=main.CurationApplyBody(name="Jazz",version="v",snapshot="s",
        operations=[{"type":"add","candidate_id":"3"}],
        plan_id="plan",approved=True)
    with pytest.raises(HTTPException) as exc:
        main.curation_apply(b,SimpleNamespace(headers={"origin":"http://evil.example"},
            base_url="http://media-server:5051/"))
    assert exc.value.status_code==403
    assert calls==[]
    result=main.curation_apply(b,SimpleNamespace(headers={"origin":"http://media-server:5051"},
        base_url="http://media-server:5051/"))
    assert result["verified"] is True
    assert calls[0]["approved"] is True


def test_plan_then_apply_uses_same_payload_and_id(monkeypatch):
    plex, music, library, definitions, live, before=context()
    monkeypatch.setattr(main,"_curation_context",lambda name:context())
    version=fingerprint(definitions)
    snap=signature(before)
    ops=[{"type":"add","candidate_id":"3"}]
    body=main.CurationPlanBody(name="Jazz",version=version,snapshot=snap,operations=ops)
    plan=main.curation_plan(body)
    assert plan["after"]==["1","2","3"]
    assert len(plan["plan_id"])==24
