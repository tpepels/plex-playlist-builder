"""Acceptance fixtures: genre-specific discovery over complete Plex/AudioMuse catalogue."""
from __future__ import annotations

import base64
import struct

import pytest

from app.playlist_generation import (
    parse_prompt, generate, genre_profiles, genre_evidence, catalogue_revision,
    diagnose, GenerationError, _style_token,
)
from app.playlist_curator import features_for
from app.audiomuse_client import readiness, feature_revision


def vec(*values):
    return base64.b64encode(struct.pack("<" + "f" * len(values), *values)).decode("ascii")


def catalogue(size=540):
    library, cache = {}, {}
    for i in range(1, size + 1):
        key = str(i)
        is_positive = i <= 5
        is_acoustic = i >= 500
        positive = is_positive or is_acoustic
        tags = ["Jazz", "Hard Bop"] if is_positive else (
            [] if is_acoustic else ["Jazz", "Cool Jazz"]
        )
        library[key] = {
            "key": key, "artist": f"Ensemble {(i-1)//3}",
            "album": f"Recording {(i-1)//3}", "album_key": str((i-1)//3),
            "title": f"Track {i}", "year": 1960,
            "genres": tags, "duration_ms": 360000,
            "disc": 1, "index": i,
        }
        v = (1., 0., 0., 0.) if positive else (0., 1., 0., 0.)
        cache[key] = {
            "embedding": vec(*v), "clap_embedding": vec(*v),
            "energy": .42 + (i % 9)*.02,
            "tempo": 100 + i % 8,
            "other_features": {"relaxed": .42, "happy": .56},
        }
    return library,cache


def intent(count=30, **extra):
    return parse_prompt(
        "Hard bop jazz with balanced artist variety and smooth transitions.",
        count=count, genre="Jazz", subgenre="Hard bop",
        overrides={"variation": "balanced", "flow": "smooth"},
        **extra,
    )


def test_structured_genre_and_subgenre_survive_prompt_interpretation():
    request = intent()
    assert request["identity"]["variation"] == "balanced"
    assert request["identity"]["flow"] == "smooth"
    assert request["criteria"]["genre"] == "jazz"
    assert request["criteria"]["subgenre"] == "hard bop"
    assert request["criteria"]["count"] == 30
    assert request["criteria"]["require_exact_count"] is True
    assert _style_token("Hard-bop") == "hard bop"
    assert _style_token("Bebop") == "bebop"


def test_30_track_hard_bop_uses_untagged_music_beyond_old_500_cutoff():
    lib, cache = catalogue()
    request = intent()
    data = generate(request, lib, cache)
    assert len(data["keys"]) == 30
    assert len(set(data["keys"])) == 30
    assert set(data["keys"]) <= set(lib)
    assert data["selection"]["catalogue"] == 540
    assert data["selection"]["shortlisted"] >= 30
    assert data["selection"]["shortlisted"] == data["selection"]["evaluated"]
    assert any(int(key) >= 500 for key in data["keys"])
    assert all(int(key)<=5 or int(key)>=500 for key in data["keys"])
    assert all(data["genre_evidence"][key]["compatible"] for key in data["keys"])
    assert all("musicnn_similarity" in data["genre_evidence"][key]["evidence"]
               for key in data["keys"])
    assert data["genre_profile"]["verified_metadata_count"] == 5
    assert len(data["diagnostics"]["transitions"]) == 29
    assert generate(request, lib, cache)["keys"] == data["keys"]


def test_negative_references_exclude_acoustically_related_nonmatching_styles():
    lib, cache = catalogue(42)
    ff = features_for(cache)
    profile = genre_profiles(lib, ff, genre="jazz", subgenre="hard bop",
                             negative_reference_track_ids=["7"])
    pos = genre_evidence("1", lib["1"], ff, profile)
    negative = genre_evidence("7", lib["7"], ff, profile)
    assert pos["compatible"]
    assert pos["evidence"]["metadata_support"] == "verified_subgenre"
    assert not negative["compatible"]
    assert negative["evidence"]["metadata_support"] == "related_subgenre"
    assert negative["evidence"]["musicnn_similarity"] < pos["evidence"]["musicnn_similarity"]


def test_manual_verified_examples_seed_untagged_genre():
    lib,cache = catalogue(42)
    for row in lib.values():
        row["genres"] = []
    ff = features_for(cache)
    with pytest.raises(GenerationError, match="Insufficient verified"):
        genre_profiles(lib, ff, genre="jazz", subgenre="hard bop")
    profile = genre_profiles(lib, ff, genre="jazz", subgenre="hard bop",
                             reference_track_ids=["1", "2"],
                             negative_reference_track_ids=["7", "8"])
    assert profile["manual_positive_count"] == 2
    assert profile["available"]
    assert genre_evidence("3",lib["3"],ff,profile)["compatible"]
    assert not genre_evidence("7",lib["7"],ff,profile)["compatible"]


def test_shortage_is_error_instead_of_unrelated_padding():
    lib,cache = catalogue(30)
    with pytest.raises(GenerationError, match="Only 5 of 30"):
        generate(intent(),lib,cache)


def test_explicit_exclusions_years_and_evidence_threshold_are_authoritative():
    lib,cache = catalogue(540)
    request = intent(year_from=1958,year_to=1962,
                     excluded_track_ids=["500"],
                     excluded_artists=["Ensemble 167"],
                     evidence_threshold="strict")
    assert request["criteria"]["years"] == [1958, 1962]
    assert request["criteria"]["evidence_threshold"] == "strict"
    result = generate(request,lib,cache)
    assert "500" not in result["keys"]
    assert all(lib[k]["artist"] != "Ensemble 167" for k in result["keys"])
    with pytest.raises(GenerationError,match="Both explicit year bounds"):
        intent(year_from=1950)
    with pytest.raises(GenerationError,match="Unsupported subgenre"):
        parse_prompt("Unknown electronic style",genre="electronic",subgenre="hardstyle")


def test_manual_reorder_recalculates_transition_diagnostics():
    lib,cache = catalogue(540)
    request = intent()
    result = generate(request,lib,cache)
    order=list(reversed(result["keys"]))
    metrics = diagnose(order,lib,cache,request["identity"],request)
    assert metrics["tracks"] == 30
    assert len(metrics["transitions"]) == 29
    assert metrics["transitions"][0]["to_id"] == order[1]
    assert metrics["transitions"][0]["from_id"] == order[0]


def test_local_audiomuse_reconciliation_counts_real_valid_features():
    lib,cache = catalogue(30)
    cache.pop("9")
    cache["10"]["embedding"] = "@@invalid@@"
    cache["orphan"] = {"embedding":vec(1.,0.,0.,0.),"energy":.5}
    state = {"available":31,"pending":0,"last_sync":"2026-11-11T10:00:00Z",
             "manifest_version":"rev1"}
    report = readiness(lib,cache,state)
    assert report["plex_tracks"] == 30
    assert report["unmatched_plex_tracks"] == 1
    assert report["orphan_audio_tracks"] == 1
    assert report["invalid_musicnn"] == 1
    assert report["matched_track_ids"] == 29
    assert report["ready"] is False
    assert report["clap_available"] == 29
    assert report["energy_available"] == 29
    assert feature_revision(cache) != feature_revision({**cache, "11":{"energy":0.99}})
    assert catalogue_revision(lib)==catalogue_revision(dict(reversed(list(lib.items()))))


def test_missing_manifest_is_unknown_not_success():
    lib,cache=catalogue(20)
    report=readiness(lib,cache,{"cached":20,"last_sync":None})
    assert report["pending"] is None
    assert report["audiomuse_available"] is None
    assert report["ready"] is False
