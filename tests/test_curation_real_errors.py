"""Real-data-inspired metadata failures and safe automatic candidate selection."""
from __future__ import annotations

from app.curation_export_audit import audit_export
from app.curation_review_rules import automatically_rankable, review_track
from app.playlist_ranking import rank_candidates


def song(key, artist="Artist", album="Album", title=None, duration=300000):
    return {"key":str(key),"artist":artist,"album":album,
            "title":title or "Song "+str(key),"album_key":artist+"|"+album,
            "duration_ms":duration,"disc":1,"index":int(key)}


def identity():
    return {"purpose":"Live jazz and standards","styles":["jazz"],
            "moods":[],"variation":"balanced","energy":"adaptive",
            "flow":"smooth","live":"prefer"}


def test_spoken_art_pepper_intro_is_excluded_from_automatic_ranking():
    library={str(i):song(i,artist="Artist "+str(i)) for i in range(1,8)}
    library["8"]=song(8,artist="Art Pepper",
        title="Spoken Introduction 6 (Live At The Village Vanguard, New York City, NY / July 28, 1977)",
        duration=100640)
    base=[str(i) for i in range(1,7)]
    result=rank_candidates(base,library,{},identity(),limit=30)
    assert [row["id"] for row in result]==["7"]
    explicit=rank_candidates(base,library,{},identity(),query="Spoken Introduction 6",limit=30)
    assert len(explicit)==1
    assert explicit[0]["id"]=="8"
    assert explicit[0]["match"] is None
    assert explicit[0]["review_flags"][0]["code"]=="likely_spoken_or_applause"


def test_short_ambient_composition_is_reviewable_but_not_excluded():
    track=song(100,artist="Supersilent",title="14.9",duration=60260)
    assert automatically_rankable(track)
    assert [x["code"] for x in review_track(track)]==["short_duration"]


def test_export_audit_checks_all_entries_and_does_not_erase_short_tracks():
    playlists={"Live Jazz":{"items":[
        {"rating_key":"1","artist":"Art Pepper","album":"Live","track":"Real Song"},
        {"rating_key":"2","artist":"Art Pepper","album":"Live",
         "track":"Spoken Introduction 6"}]},
        "Ambient":{"items":[
            {"rating_key":"3","artist":"Supersilent","album":"Album","track":"14.9"},
            {"rating_key":"3"},
            {"rating_key":"999"}]}}
    library={
        "1":{"rating_key":"1","artist":"Art Pepper","album":"Live",
             "title":"Real Song","duration_ms":"600000"},
        "2":{"rating_key":"2","artist":"Art Pepper","album":"Live",
             "title":"Spoken Introduction 6","duration_ms":"100640"},
        "3":{"rating_key":"3","artist":"Supersilent","album":"Album",
             "title":"14.9","duration_ms":"60260"},
    }
    result=audit_export(playlists,library)
    assert result["metadata_only"] and not result["audio_reviewed"]
    assert result["totals"]["missing_rating_keys"]==1
    assert result["totals"]["duplicate_rating_keys"]==1
    assert result["totals"]["spoken_or_applause_title_candidates"]==1
    assert result["totals"]["short_tracks_for_review"]==3
    assert result["playlists"]["Ambient"]["tracks"]==3
    assert not result["playlists"]["Ambient"]["metadata_mismatches"]
