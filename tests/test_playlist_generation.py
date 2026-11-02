"""Full music-prompt generation: constraints, selection, ordering, coverage and edits."""
from __future__ import annotations
import base64
import struct

import pytest

from app.playlist_generation import (
    GenerationError, parse_prompt, generate, diagnose, _constraints,
)
from app.playlist_curator import compute_operations


def b64(*numbers):
    return base64.b64encode(struct.pack("<"+"f"*len(numbers),*numbers)).decode()


def library(n=45):
    tracks={}
    for i in range(1,n+1):
        tracks[str(i)]={
            "key":str(i),"artist":f"Performer {(i-1)//3}",
            "album":f"Live at the Jazz Club {(i-1)//3}",
            "album_key":str(100+(i-1)//3),
            "title":f"Improvisation {i}",
            "year":1957+i%7,"genres":["Jazz"],
            "duration_ms":9*60000+(i%4)*60000,"disc":1,"index":i
        }
    tracks["43"]={**tracks["43"],"title":"Spoken Introduction 43"}
    tracks["44"]={**tracks["44"],"year":None}
    tracks["45"]={**tracks["45"],"year":1985}
    return tracks


def features(n=45):
    return {str(i):{
        "embedding":b64(1.,0.,0.,0.) if i%3 else b64(.8,.6,0.,0.),
        "clap_embedding":b64(1.,0.,0.,0.),
        "energy": .25+(i%8)*.07,
        "other_features":{"relaxed":.72,"happy":.55},
        "tempo":100+(i%6)*4,
    } for i in range(1,n+1)}


def test_prompt_interprets_period_live_improvisation_and_mood():
    p=parse_prompt("Intimate live jazz, 1955–1965, long improvisations and warm club atmosphere",
                   count=12)
    assert p["criteria"]["years"]==[1955,1965]
    assert p["criteria"]["live_required"] is True
    assert p["criteria"]["minimum_duration_ms"]==480000
    assert p["criteria"]["count"]==12
    assert p["identity"]["styles"]==["jazz"]
    assert "intimate" in p["identity"]["moods"]
    assert p["interpretation"]["metadata_year_kind"]=="album_release_year"
    assert any("not verified concert" in w for w in p["interpretation"]["warnings"])
    assert any("not directly measured" in w for w in p["interpretation"]["warnings"])


def test_prompt_validates_size_duration_years_and_unrecognized_brief():
    for kwargs in ({"count":0},{"count":121},{"minutes":14},{"minutes":901}):
        with pytest.raises(GenerationError):
            parse_prompt("Intimate live jazz",**kwargs)
    with pytest.raises(GenerationError):
        parse_prompt("Jazz improvisations 1980-1950")
    with pytest.raises(GenerationError):
        parse_prompt("x")
    assert parse_prompt("Soul 1960s, 2 hours and 12 tracks")["criteria"]["years"]==[1960,1969]
    assert parse_prompt("Soul 1960s, 2 hours and 12 tracks")["criteria"]["target_minutes"]==120


def test_complete_generation_uses_entire_catalogue_and_verified_constraints():
    lib=library()
    request=parse_prompt("Intimate live jazz, 1955–1965, long improvisations",count=14)
    result=generate(request,lib,features())
    keys=result["keys"]
    assert len(keys)==14 and len(set(keys))==14
    assert set(keys).isdisjoint({"43","44","45"})
    assert all(_constraints(lib[k],request) is None for k in keys)
    assert result["selection"]["catalogue"]==45
    assert result["selection"]["excluded"]["missing_year"]==1
    assert result["selection"]["excluded"]["outside_years"]==1
    assert result["selection"]["excluded"]["spoken_or_applause"]==1
    assert len(result["tracks"])==len(keys)
    assert result["diagnostics"]["analyzed"]==14
    assert set(result["diagnostics"]["metrics"])=={
        "coherence","variation","energy","flow","intent"
    }
    assert generate(request,lib,features())["keys"]==keys


def test_no_hallucinated_metadata_when_audio_or_year_is_missing():
    lib=library(15)
    for row in lib.values():row["year"]=None
    request=parse_prompt("Warm live jazz from 1955-1965",count=10)
    with pytest.raises(GenerationError,match="No tracks satisfy"):
        generate(request,lib,{})
    without_year=parse_prompt("Warm live jazz extended improvisations",count=8)
    result=generate(without_year,lib,{})
    assert result["diagnostics"]["unmeasured_tracks"]==len(result["keys"])
    assert result["diagnostics"]["metrics"]["coherence"]["score"] is None
    assert any("metadata" in warning.lower() for warning in result["warnings"])


def test_selection_honors_explicit_genre_and_duration():
    lib=library(30)
    lib["1"]["genres"]=["Rock"]
    lib["1"]["album"]="Unknown recording"
    lib["2"]["duration_ms"]=90*1000
    request=parse_prompt("Live jazz long improvisations",count=8)
    built=generate(request,lib,features())
    assert "1" not in built["keys"] and "2" not in built["keys"]
    assert all(lib[key]["duration_ms"]>=480000 for key in built["keys"])


def test_manual_edits_reorder_replace_and_preserve_quality_diagnostics():
    lib=library()
    request=parse_prompt("Live jazz warm intimate club",count=9)
    result=generate(request,lib,features())
    before=result["keys"]
    replacement=next(k for k in lib if k not in before)
    edited=compute_operations(before,[
        {"type":"remove","track_id":before[1]},
        {"type":"add","candidate_id":replacement,"after_id":before[0]},
        {"type":"move","track_id":before[0],"after_id":before[2]}
    ],lib)
    assert len(edited)==len(before)
    assert replacement in edited
    assert edited!=before
    quality=diagnose(edited,lib,features(),request["identity"],request)
    assert quality["tracks"]==len(edited)
    assert "flow" in quality["metrics"]


def test_too_few_matching_tracks_is_explicit_not_filled_from_wrong_genre():
    lib=library(6)
    req=parse_prompt("Live jazz, long improvisations",count=18)
    result=generate(req,lib,features(6))
    assert len(result["keys"])==6
    assert any("Only 6 of 18" in w for w in result["warnings"])
