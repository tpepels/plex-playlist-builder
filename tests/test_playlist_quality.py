"""Observed, explainable music composition metrics without invented analysis."""
import base64
import struct

from app.playlist_quality import evaluate


def vector(*values):
    return base64.b64encode(struct.pack("<"+"f"*len(values),*values)).decode()


def catalogue():
    return {
        str(i):{"artist":"Artist A" if i<=4 else "Artist B",
                "album_key":"A" if i<=3 else "B",
                "album":"Album A" if i<=3 else "Album B",
                "title":"Track "+str(i),"duration_ms":200000}
        for i in range(1,7)
    }


def test_measured_coherence_energy_and_existing_overlap():
    raw={str(i):{"embedding":vector(.2,.4,.3,.1),
                 "clap_embedding":vector(.2,.4,.3,.1),
                 "energy":.1 if i<=3 else .9,
                 "tempo":120} for i in range(1,7)}
    overlap={"Similar":{"items":[{"rating_key":str(i)} for i in range(1,6)]}}
    result=evaluate([str(i) for i in range(1,7)],catalogue(),raw,overlap)
    assert result["tracks"]==6
    assert result["unique_artists"]==2
    assert result["unique_albums"]==2
    assert result["max_artist_share"]==.6667
    assert result["full_embedding_coverage"]=={"tracks":6,"total":6}
    assert result["audio_similarity"]["measured_transitions"]==5
    assert result["energy_changes"]["maximum"]==.8
    assert result["existing_overlaps"][0]["common_tracks"]==5
    assert result["existing_overlaps"][0]["fraction_of_proposal"]==.8333
    assert "Playlist substantially overlaps an existing collection" in result["warnings"]


def test_missing_vectors_remain_unmeasured_not_optimistically_scored():
    raw={"1":{"energy":.5}, "2":{"embedding":"invalid"}}
    result=evaluate(["1","2","3"],catalogue(),raw)
    assert result["full_embedding_coverage"]=={"tracks":0,"total":3}
    assert result["audio_similarity"]["mean"] is None
    assert result["audio_similarity"]["measured_transitions"]==0
    assert result["energy_changes"]["maximum"] is None
    assert "Missing MusicNN or CLAP embeddings; coherence conclusions are limited" in result["warnings"]
