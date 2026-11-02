"""Intent and contextual ranking regression tests; no Plex server required."""
from __future__ import annotations

import base64
import struct
import pytest

from app import playlist_identity as identity
from app.playlist_ranking import rank_candidates
from app.playlist_curator import compute_operations


def vec(*items):
    return base64.b64encode(struct.pack("<"+"f"*len(items),*items)).decode()


def row(key, artist, album, title):
    return {"key":str(key),"artist":artist,"album":album,
            "title":title,"album_key":artist+"|"+album,"disc":1,"index":int(key)}


def audio(values):
    return {str(key):{"embedding":vec(*vector),"clap_embedding":vec(*vector),
        "energy":energy,"tempo":tempo,
        "other_features":{"relaxed":relaxed,"aggressive":1-relaxed}}
        for key,vector,energy,tempo,relaxed in values}


def intent(**updates):
    basic={"purpose":"Intimate jazz","styles":["jazz"],
           "moods":["relaxed"],"variation":"balanced",
           "energy":"adaptive","flow":"smooth","live":"neutral"}
    return {**basic,**updates}


def test_infer_multigenre_mix_without_conflating_with_single_genre():
    library={"1":row(1,"Miles","Kind of Blue","So What")}
    value,evidence=identity.infer("Evening — Jazz, Beats & Soul in the Same Room",
                                  "",["1"],library,{})
    assert set(value["styles"])=={"jazz","hip hop","soul"}
    assert value["variation"]=="eclectic"
    assert value["energy"]=="adaptive"
    assert evidence["analyzed_tracks"]==0
    assert "name" in evidence["basis"]


def test_infer_live_mood_energy_and_expose_source():
    library={"1":row(1,"Miles","Kind of Blue","So What")}
    value,_=identity.infer("Live Jazz — Late Night Club",
                           "Warm, intimate and quiet",["1"],library,{})
    assert value["styles"]==["jazz"]
    assert set(value["moods"]) >= {"warm","intimate","relaxed"}
    assert value["live"]=="prefer"
    assert value["energy"]=="low"
    unknown,_=identity.infer("Playlist 001","",["1"],library,{})
    assert unknown["styles"]==[]
    assert unknown["moods"]==[]
    assert unknown["live"]=="neutral"


def test_manual_identity_is_persistent_revisioned_and_separate_from_yaml(tmp_path,monkeypatch):
    monkeypatch.setattr(identity,"IDENTITIES_FILE",tmp_path/"identities.json")
    data=intent(variation="eclectic",flow="varied",styles=["jazz","soul"])
    original=identity.revision(None)
    identity.put_override("Jazz & Soul",data,original)
    saved=identity.get_override("Jazz & Soul")
    assert saved==data
    state=identity.identity("Jazz & Soul","",[],{},{})
    assert state["source"]=="manual"
    assert state["values"]["variation"]=="eclectic"
    with pytest.raises(identity.IdentityConflict,match="changed elsewhere"):
        identity.put_override("Jazz & Soul",data,original)
    updated=intent(moods=["warm","intimate"])
    identity.put_override("Jazz & Soul",updated,state["revision"])
    state=identity.identity("Jazz & Soul","",[],{},{})
    identity.delete_override("Jazz & Soul",state["revision"])
    assert identity.get_override("Jazz & Soul") is None
    assert identity.identity("Jazz & Soul","",[],{},{} )["source"]=="inferred"


@pytest.mark.parametrize("change",[
    {"variation":"random"},
    {"energy":"very high"},
    {"flow":"shuffle"},
    {"live":"maybe"},
    {"moods":["unknown"]},
    {"styles":["x"]*13},
    {"purpose":"x"*401},
])
def test_identity_validation_rejects_incomplete_or_unrecognized_fields(change):
    with pytest.raises(ValueError):
        identity.validate({**intent(),**change})
    with pytest.raises(ValueError):
        identity.validate({"variation":"balanced"})


def test_corrupt_identity_file_is_not_silently_overwritten(tmp_path,monkeypatch):
    file=tmp_path/"identities.json"
    file.write_text("{broken")
    monkeypatch.setattr(identity,"IDENTITIES_FILE",file)
    with pytest.raises(identity.IdentityConflict,match="unreadable"):
        identity.get_override("Jazz")
    with pytest.raises(identity.IdentityConflict,match="unreadable"):
        identity.put_override("Jazz",intent(),identity.revision(None))
    assert file.read_text()=="{broken"


def test_adding_new_artist_improves_diversity_in_balanced_playlist():
    library={str(i):row(i,"Same Artist","Same Album",f"Track {i}") for i in range(1,7)}
    library["7"]=row(7,"Same Artist","Other Album","Another")
    library["8"]=row(8,"Different Artist","Different Album","Another")
    feature=audio([(i,(1.,0.,0.,0.),.5,100.,.6) for i in range(1,9)])
    choices=rank_candidates(["1","2","3","4","5","6"],library,feature,intent(moods=[]),limit=4)
    assert choices[0]["id"]=="8"
    assert choices[0]["match"] is not None
    assert "artist/album diversity" in choices[0]["evidence"]


def test_mood_and_energy_intent_change_candidate_order():
    library={str(i):row(i,"Artist"+str(i),"Album"+str(i),"Track "+str(i)) for i in range(1,9)}
    records=[(i,(1.,0.,0.,0.),.35,100.,.6) for i in range(1,7)]
    records += [(7,(1.,0.,0.,0.),.20,100.,.95),
                (8,(1.,0.,0.,0.),.83,100.,.1)]
    feature=audio(records)
    base=[str(i) for i in range(1,7)]
    quiet=rank_candidates(base,library,feature,
        intent(energy="low",moods=["relaxed"]),limit=2)
    loud=rank_candidates(base,library,feature,
        intent(energy="high",moods=["energetic"]),limit=2)
    assert quiet[0]["id"]=="7"
    assert loud[0]["id"]=="8"


def test_rankings_consider_adjacent_tracks_and_return_insert_anchor():
    library={str(i):row(i,f"Artist{i}",f"Album{i}",f"Track {i}") for i in range(1,7)}
    records=[
        (1,(1.,0.,0.,0.),.1,100.,.5),
        (2,(1.,0.,0.,0.),.1,100.,.5),
        (3,(1.,0.,0.,0.),.8,100.,.5),
        (4,(1.,0.,0.,0.),.8,100.,.5),
        (5,(1.,0.,0.,0.),.9,100.,.5),
        (6,(1.,0.,0.,0.),.3,100.,.5),
    ]
    feature=audio(records)
    choices=rank_candidates(["1","2","3","4"],library,feature,
        intent(flow="smooth",moods=[]),query="Track 5",limit=1)
    assert choices[0]["id"]=="5"
    assert choices[0]["suggested_after_id"] in ("3","4")
    anchor=choices[0]["suggested_after_id"]
    after=compute_operations(["1","2","3","4"],
        [{"type":"add","candidate_id":"5","after_id":anchor}],library)
    assert after.index("5")>=3
    assert after.index("5")<=4


def test_unanalyzed_track_is_eligible_for_search_but_never_falsely_scored():
    library={str(i):row(i,f"Artist{i}","Album",f"Track {i}") for i in range(1,8)}
    feature=audio([(i,(1.,0.,0.,0.),.5,100.,.5) for i in range(1,7)])
    results=rank_candidates(["1","2","3","4","5","6"],library,feature,
        intent(),limit=5)
    assert len(results)==1
    assert results[0]["id"]=="7"
    assert results[0]["match"] is None
    assert results[0]["confidence"]=="unscored"
    assert results[0]["reasons"]==["No comparable audio embedding"]


def test_named_artist_playlist_avoids_unrelated_artist_suggestions():
    library={str(i):row(i,"Bill Evans","Explorations",f"Track {i}") for i in range(1,7)}
    library["7"]=row(7,"Bill Evans","At the Village Vanguard","Waltz")
    library["8"]=row(8,"Other Pianist","Other Album","Waltz")
    features=audio([(i,(1.,0.,0.,0.),.5,100.,.5) for i in range(1,9)])
    matches=rank_candidates(["1","2","3","4","5","6"],library,features,
        intent(purpose="Bill Evans piano",moods=[]),limit=2)
    assert matches[0]["id"]=="7"
    assert matches[0]["match"]>matches[1]["match"]


def test_high_confidence_requires_playlist_coverage():
    library={str(i):row(i,"Artist"+str(i),"Album"+str(i),"Track "+str(i)) for i in range(1,16)}
    features=audio([(i,(1.,0.,0.,0.),.5,100.,.5) for i in range(1,16)])
    strong=rank_candidates([str(i) for i in range(1,14)],library,features,
        intent(),limit=1)
    assert strong[0]["confidence"]=="high"
    sparse=rank_candidates([str(i) for i in range(1,14)],library,
        audio([(i,(1.,0.,0.,0.),.5,100.,.5) for i in (1,2,3,14)]),
        intent(),limit=1)
    assert sparse[0]["confidence"]=="low"


def test_inferred_identity_uses_observed_genres_energy_and_arc():
    library={str(i):{**row(i,"Various "+str(i),"Release "+str(i),"Track "+str(i)),
                      "genres":["Jazz","Soul"]} for i in range(1,9)}
    energies=[.20,.22,.75,.80,.80,.78,.19,.20]
    ff={str(i):{"musicnn":(1.,0.,0.,0.),"energy":energies[i-1],
                 "mood":{"relaxed":.82},"tempo":90} for i in range(1,9)}
    inferred,evidence=identity.infer("Evening songs","",list(library),library,ff)
    assert "jazz" in inferred["styles"] and "soul" in inferred["styles"]
    assert inferred["flow"]=="arc"
    assert inferred["energy"]=="medium"
    assert "relaxed" in inferred["moods"]
    assert evidence["genres_observed"]==8
    assert evidence["energy_tracks"]==8
    assert "AudioMuse" in evidence["basis"]


def test_declared_styles_use_only_explicit_plex_genres():
    library={str(i):row(i,"Artist "+str(i),"Album "+str(i),"Track "+str(i))
             for i in range(1,9)}
    library["7"]["genres"]=["Jazz"]
    library["8"]["genres"]=["Rock"]
    ff=audio([(i,(1.,0.,0.,0.),.5,100.,.5) for i in range(1,9)])
    matches=rank_candidates(["1","2","3","4","5","6"],library,ff,
        intent(styles=["jazz"],moods=[]),limit=2)
    assert matches[0]["id"]=="7"
    assert "Plex genre metadata" in matches[0]["evidence"]
    assert matches[0]["match"]>matches[1]["match"]


def test_ranked_add_inserts_after_proposed_track_and_rejects_missing_anchor():
    library={str(i):row(i,"Artist","Album","Track "+str(i)) for i in range(1,5)}
    assert compute_operations(["1","2"],
        [{"type":"add","candidate_id":"3","after_id":"1"}],library)==["1","3","2"]
    assert compute_operations(["1","2"],
        [{"type":"add","candidate_id":"3","after_id":None}],library)==["3","1","2"]
    from app.playlist_curator import CurationConflict
    with pytest.raises(CurationConflict,match="anchor"):
        compute_operations(["1","2"],
            [{"type":"add","candidate_id":"3","after_id":"99"}],library)
