"""Step 3: all five before/after signals, coverage, uncertainty and no-op invariance."""
from __future__ import annotations
import base64
import struct

import pytest

from app.curation_impact import compare


def b64(v):
    return base64.b64encode(struct.pack("<4f",*v)).decode()


def library():
    return {
        str(i):{"key":str(i),"artist":("Artist A" if i<6 else "Artist "+str(i)),
                "album_key":("Album A" if i<6 else "Album "+str(i)),
                "album":"Album","title":"Track "+str(i),"genres":["Jazz"],
                "disc":1,"index":i}
        for i in range(1,12)
    }


def cache():
    result={}
    for i in range(1,12):
        values=(1,0,0,0) if i!=11 else (0,1,0,0)
        result[str(i)]={"embedding":b64(values),
                        "clap_embedding":b64(values),
                        "energy":.35 if i<11 else .91,
                        "other_features":{"relaxed":.75 if i<11 else .1},
                        "tempo":100}
    return result


def intent(mode="balanced",flow="smooth"):
    return {"purpose":"Jazz","styles":["jazz"],"moods":["relaxed"],
            "variation":mode,"energy":"low","flow":flow,"live":"neutral"}


def test_same_sequence_is_zero_delta_in_all_scored_dimensions():
    ids=[str(i) for i in range(1,10)]
    result=compare(ids,ids,library(),cache(),intent())
    assert result["scored_dimensions"]==5
    assert all(r["delta"]==0 for r in result["metrics"].values())
    assert all(r["direction"]=="unchanged" for r in result["metrics"].values())


def test_replace_outlier_changes_coherence_energy_flow_and_intent():
    ids=[str(i) for i in range(1,9)]+["11"]
    improved=ids[:-1]+["9"]
    result=compare(ids,improved,library(),cache(),intent())
    assert result["metrics"]["coherence"]["delta"]>0
    assert result["metrics"]["flow"]["delta"]>0
    assert result["metrics"]["intent"]["delta"]>0
    assert result["metrics"]["energy"]["delta"]>0
    assert "before" in result["metrics"]["variation"]
    assert "after" in result["metrics"]["variation"]


def test_incomplete_features_cannot_claim_improvement():
    cache_with_gap={k:v for k,v in cache().items() if int(k)<5}
    ids=[str(i) for i in range(1,10)]
    after=ids[:-1]+["11"]
    report=compare(ids,after,library(),cache_with_gap,intent())
    assert report["comparable"] is False
    assert report["metrics"]["coherence"]["delta"] is None
    assert report["metrics"]["coherence"]["direction"]=="unknown"
    assert report["metrics"]["coherence"]["uncertainty"]
    assert report["metrics"]["variation"]["delta"] is not None


def test_unanalyzed_candidate_drops_coverage_and_withholds_delta():
    ids=[str(i) for i in range(1,7)]
    raw=cache()
    raw.pop("11")
    result=compare(ids,ids[:-1]+["11"],library(),raw,intent())
    assert result["metrics"]["coherence"]["before"]["coverage"]==1
    assert result["metrics"]["coherence"]["after"]["coverage"]<1
    assert result["metrics"]["coherence"]["after"]["status"]=="insufficient" or (
        result["metrics"]["coherence"]["after"]["score"] is not None)
    assert "Incomplete feature coverage" in result["metrics"]["coherence"]["uncertainty"]
    assert result["metrics"]["coherence"]["delta"] is None
    assert "Introduced recording lacks data for this dimension" in result["metrics"]["coherence"]["uncertainty"]


def test_reorder_changes_flow_but_does_not_change_artist_diversity():
    data=library()
    raw=cache()
    # Interleave steady and intense passages.
    for i in (2,4,6,8,10):
        raw[str(i)]["energy"] = .9
    ids=[str(i) for i in range(1,11)]
    after=ids[::2]+ids[1::2]
    report=compare(ids,after,data,raw,intent())
    assert report["metrics"]["variation"]["delta"]==0
    assert report["metrics"]["energy"]["delta"]==0
    assert report["metrics"]["flow"]["delta"]>0


def test_identity_changes_diversity_target_without_changing_raw_diversity():
    ids=[str(i) for i in range(1,10)]
    a=compare(ids,ids,library(),cache(),intent("focused"))
    b=compare(ids,ids,library(),cache(),intent("eclectic"))
    assert a["metrics"]["variation"]["before"]["detail"] == (
        b["metrics"]["variation"]["before"]["detail"])
    assert a["metrics"]["variation"]["before"]["score"] != (
        b["metrics"]["variation"]["before"]["score"])


def test_comparison_rejects_catalogue_mismatch():
    with pytest.raises(ValueError,match="missing"):
        compare(["1","999"],["1","2"],library(),cache(),intent())
