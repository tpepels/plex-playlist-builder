"""End-to-end route tests for Step 3 impact and Step 4 human validation."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import main
from app import curation_validation as val
from app.config_store import fingerprint
from app.playlist_curator import signature


def track(i):
    return SimpleNamespace(ratingKey=i,parentRatingKey=i,
        grandparentTitle="Artist "+str(i),parentTitle="Album "+str(i),
        title="Track "+str(i),parentIndex=1,index=i,TYPE="track")


class Plex:
    def playlists(self,playlistType):
        return [SimpleNamespace(title="After Hours Jazz",smart=False,
                 items=lambda:[track(i) for i in range(1,7)],ratingKey=222)]


def context(name):
    library={str(i):{"key":str(i),"artist":"Artist "+str(i),
        "album_key":"Album "+str(i),"album":"Album "+str(i),
        "title":"Track "+str(i),"disc":1,"index":i,"genres":["Jazz"]}
        for i in range(1,9)}
    spec={name:{"description":"Relaxed jazz","items":[{"rating_key":str(i)} for i in range(1,7)]}}
    live=SimpleNamespace(ratingKey=222,items=lambda:[track(i) for i in range(1,7)])
    return Plex(),None,library,spec,live,[str(i) for i in range(1,7)]


def patch_context(monkeypatch):
    monkeypatch.setattr(main,"_curation_context",lambda name:context(name))
    monkeypatch.setattr(main,"cached_features",lambda:({},
         {"cached":0,"last_sync":None}))
    monkeypatch.setattr(main,"playlist_identity",lambda name,desc,keys,lib,ff:{
        "values":{"purpose":"Jazz","styles":["jazz"],"moods":[],
            "variation":"balanced","energy":"adaptive",
            "flow":"smooth","live":"neutral"},
        "revision":"test-id","source":"manual","suggested":{},"evidence":{}
    })


def test_staged_plan_contains_five_component_impact_and_uncertainty(monkeypatch):
    patch_context(monkeypatch)
    ids=[str(i) for i in range(1,7)]
    definitions=context("Jazz")[3]
    body=main.CurationPlanBody(name="Jazz",version=fingerprint(definitions),
        snapshot=signature(ids),operations=[{"type":"replace","track_id":"6","candidate_id":"7"}],
        identity_revision="test-id")
    plan=main.curation_plan(body)
    assert plan["after"][-1]=="7"
    assert set(plan["impact"]["metrics"])=={"coherence","variation","energy","flow","intent"}
    assert plan["impact"]["metrics"]["coherence"]["delta"] is None
    assert plan["impact"]["metrics"]["variation"]["delta"] is not None
    assert plan["impact"]["identity_revision"]=="test-id"


def test_rejects_stale_identity_before_review_or_approval(monkeypatch):
    patch_context(monkeypatch)
    ids=[str(i) for i in range(1,7)]
    b=main.CurationPlanBody(name="Jazz",version=fingerprint(context("Jazz")[3]),
        snapshot=signature(ids),operations=[{"type":"add","candidate_id":"7"}],
        identity_revision="stale")
    with pytest.raises(HTTPException) as exc:
        main.curation_impact(b)
    assert exc.value.status_code==409
    called=[]
    monkeypatch.setattr(main,"apply_approved",
        lambda *args,**kwargs:called.append(True))
    request=SimpleNamespace(headers={"origin":"http://example.com"},
                            base_url="http://example.com/")
    with pytest.raises(HTTPException) as rejected:
        main.curation_apply(main.CurationApplyBody(**b.model_dump(),
             plan_id="ignored",approved=True),request)
    assert rejected.value.status_code==409
    assert not called


def test_candidate_recommendations_contain_impact_for_every_result(monkeypatch,tmp_path):
    patch_context(monkeypatch)
    monkeypatch.setattr(val,"FILE",tmp_path/"reviews.sqlite")
    result=main.get_curation_candidates("Jazz",target="6",q="",limit=10)
    assert len(result["candidates"])==2
    assert all(set(row["impact"]["metrics"])=={"coherence","variation","energy","flow","intent"}
        for row in result["candidates"])
    assert result["calibration"]["reviewed"]==0


def test_human_feedback_route_requires_origin_attestation_and_stable_snapshot(
    monkeypatch,tmp_path):
    patch_context(monkeypatch)
    monkeypatch.setattr(val,"FILE",tmp_path/"reviews.sqlite")
    monkeypatch.setattr(main,"record_validation",val.record)
    monkeypatch.setattr(main,"validation_calibration",val.calibration)
    monkeypatch.setattr(main,"validation_feedback",val.feedback_for)
    ids=[str(i) for i in range(1,7)]
    body=main.ValidationBody(name="Jazz",target_id="6",candidate_id="7",
       verdict="reject",issue="mood",note="Too energetic",listened=True,
       candidate_played_seconds=18,original_played_seconds=21,
       identity_revision="test-id",snapshot=signature(ids))
    other=SimpleNamespace(headers={"origin":"https://evil.example"},
                          base_url="http://example.com/")
    with pytest.raises(HTTPException) as exc:
        main.submit_playlist_validation(body,other)
    assert exc.value.status_code==403
    assert val.reviews("Jazz")==[]
    own=SimpleNamespace(headers={"origin":"http://example.com"},
                        base_url="http://example.com/")
    result=main.submit_playlist_validation(body,own)
    assert result["saved"] is True
    assert val.reviews("Jazz")[0]["issue"]=="mood"
    assert result["calibration"]["reviewed"]==1
    with pytest.raises(HTTPException) as exc:
        main.submit_playlist_validation(body.model_copy(update={"snapshot":"old"}),own)
    assert exc.value.status_code==409
    assert len(val.reviews("Jazz"))==1
    with pytest.raises(HTTPException) as too_short:
        main.submit_playlist_validation(body.model_copy(update={
            "candidate_played_seconds":5}),own)
    assert too_short.value.status_code==422


def test_cross_genre_validation_uses_plex_not_synthetic_assumptions(
    monkeypatch,tmp_path):
    monkeypatch.setattr(val,"FILE",tmp_path/"reviews.sqlite")
    definitions={
      "After Hours Jazz":{"description":"Jazz","items":[]},
      "Ambient - Low Light & Deep Listening":{"description":"Ambient","items":[]},
      "Deep Funk & Soul":{"description":"Soul","items":[]},
      "Evening - Jazz, Beats & Soul in the Same Room":{"description":"Mixed","items":[]},
    }
    monkeypatch.setattr(main,"_configured_playlists",lambda:definitions)
    monkeypatch.setattr(main,"get_override",lambda name:None)
    monkeypatch.setattr(main,"connect",lambda:(Plex(),object()))
    monkeypatch.setattr(main,"get_library",lambda music:context("Jazz")[2])
    monkeypatch.setattr(main,"cached_features",lambda:({},{}))
    results=main.get_curation_validation()
    assert len(results["categories"])==4
    assert {r["category"] for r in results["categories"]}=={"jazz","ambient","soul","mixed"}
    assert results["human_validation_complete"] is False
    assert next(r for r in results["categories"] if r["category"]=="jazz")["tracks"]==6
    assert next(r for r in results["categories"] if r["category"]=="ambient")["status"]=="missing_in_plex"



def test_merged_routes_support_playlist_titles_containing_slashes():
    from starlette.routing import Match
    for method,path,expected_name in [
       ("GET","/api/curation/playlist/Still / Minimal","Still / Minimal"),
       ("GET","/api/curation/identity/Still / Minimal","Still / Minimal"),
       ("PUT","/api/curation/identity/Still / Minimal","Still / Minimal"),
       ("POST","/api/curation/identity/Still / Minimal/reset","Still / Minimal"),
       ("GET","/api/curation/validation/Still / Minimal","Still / Minimal"),
    ]:
        scope={"type":"http","method":method,"path":path,"root_path":""}
        matching=[route for route in main.app.routes
                  if route.matches(scope)[0] == Match.FULL]
        assert matching, (method,path)
        assert matching[0].matches(scope)[1]["path_params"]["name"]==expected_name



def test_validation_queue_is_reproducible_and_requests_actual_audio_review(monkeypatch,tmp_path):
    patch_context(monkeypatch)
    monkeypatch.setattr(val,"FILE",tmp_path/"queue.sqlite")
    first=main.get_curation_validation_queue("Jazz")
    second=main.get_curation_validation_queue("Jazz")
    assert first==second
    assert len(first["targets"])>=4
    assert len({row["id"] for row in first["targets"]})==len(first["targets"])
    assert all(row["id"] in [str(i) for i in range(1,7)] for row in first["targets"])
    assert all(row["reason"]=="stratified playlist sample" for row in first["targets"])
    assert first["reviewed"]==0
    assert first["target_review_count"]==8
    assert first["audio_coverage"]==0
