"""Fast, offline AudioMuse cache, synchronization and coherence checks."""
import base64
import json
import struct

import pytest

from app import audiomuse_client as client
from app.audiomuse_coherence import coherence_report, vector_from_feature


def vector(*numbers):
    return base64.b64encode(struct.pack("<" + "f" * len(numbers), *numbers)).decode("ascii")


def test_valid_and_invalid_musicnn_vectors():
    valid = vector(1., 0., 0., 0.)
    assert vector_from_feature({"embedding": valid}) == (1., 0., 0., 0.)
    assert vector_from_feature({"embedding": "not base64@"}) is None
    assert vector_from_feature({"embedding": vector(0., 0., 0., 0.)}) is None


def test_score_gated_on_coverage():
    features = {str(i): {"title": str(i), "embedding": vector(1., 0., 0., 0.)}
                for i in range(1, 6)}
    playlists = {"Too sparse": {"items": [{"rating_key": str(i)} for i in range(1, 11)]}}
    report = coherence_report(playlists, features, {"cached": len(features)})
    item = report["playlists"][0]
    assert item["status"] == "insufficient"
    assert item["mean_pairwise_cosine"] is None
    assert item["review_candidates"] == []


def test_relative_outlier_ranked_without_removal():
    features = {str(i): {"title": f"track {i}", "artist": "artist",
                         "embedding": vector(1., 0., 0., 0.)}
                for i in range(1, 9)}
    features["9"] = {"title": "different", "artist": "artist",
                     "embedding": vector(0., 1., 0., 0.)}
    report = coherence_report(
        {"Group": {"items": [{"rating_key": str(i)} for i in range(1, 10)]}},
        features, {"cached": len(features)},
    )
    item = report["playlists"][0]
    assert item["status"] == "ready"
    assert item["review_candidates"][0]["id"] == "9"
    assert 0 < item["mean_pairwise_cosine"] < 1


def test_album_level_item_does_not_get_false_coverage():
    feature = {"title": "one", "embedding": vector(1., 0., 0., 0.)}
    playlists = {"Albums": {"items": [{"artist": "X", "album": "Y"}] +
                             [{"rating_key": str(i)} for i in range(1, 9)]}}
    report = coherence_report(playlists, {str(i): feature for i in range(1, 9)},
                              {"cached": 8})
    entry = report["playlists"][0]
    assert entry["unresolved_items"] == 1
    assert entry["status"] == "insufficient"


def test_incremental_manifest_and_local_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "CACHE_FILE", tmp_path / "cache.sqlite")
    monkeypatch.setattr(client, "_settings", lambda: ("http://audio", "secret"))
    state = {"tracks": {"10": "fp-1", "11": "fp-2"}}
    calls = []

    def fake_get(url, token, params):
        calls.append(params)
        if params.get("fields") == "index":
            items = [{"id": k, "fp": v} for k, v in state["tracks"].items()]
            return {"tracks": items, "total_tracks": len(items),
                    "provider_type": "plex", "has_more": False}
        keys = params["ids"].split(",")
        return {"tracks": [{"id": k, "fp": state["tracks"][k], "tempo": 100,
                            "embedding": vector(1., 0., 0., 0.)} for k in keys],
                "provider_type": "plex"}
    monkeypatch.setattr(client, "_get", fake_get)
    first = client.synchronize()
    assert first["new_or_updated"] == 2
    assert first["cached"] == 2
    data, meta = client.cached_features()
    assert set(data) == {"10", "11"}
    assert meta["last_sync"]

    calls.clear()
    second = client.synchronize()
    assert second["new_or_updated"] == 0
    assert len(calls) == 1  # Fingerprint match avoids full payload fetch.

    state["tracks"] = {"10": "fp-new"}
    third = client.synchronize()
    assert third["new_or_updated"] == 1
    assert third["removed"] == 1
    data, _ = client.cached_features()
    assert set(data) == {"10"}


def test_failed_batch_preserves_existing_data(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "CACHE_FILE", tmp_path / "cache.sqlite")
    monkeypatch.setattr(client, "_settings", lambda: ("http://audio", "secret"))
    state = {"fp": "original", "fail": False}

    def fake_get(url, token, params):
        if params.get("fields") == "index":
            return {"tracks": [{"id": "42", "fp": state["fp"]}],
                    "total_tracks": 1, "provider_type": "plex", "has_more": False}
        if state["fail"]:
            return {"tracks": [], "provider_type": "plex"}
        return {"tracks": [{"id": "42", "fp": state["fp"], "tempo": 111}],
                "provider_type": "plex"}
    monkeypatch.setattr(client, "_get", fake_get)
    client.synchronize()
    state["fp"] = "updated"
    state["fail"] = True
    with pytest.raises(RuntimeError, match="partial"):
        client.synchronize()
    tracks, _ = client.cached_features()
    assert tracks["42"]["tempo"] == 111


def test_missing_token_is_not_logged(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "TOKEN_FILE", tmp_path / "nonexistent")
    monkeypatch.delenv("AUDIOMUSE_API_TOKEN", raising=False)
    monkeypatch.setenv("AUDIOMUSE_URL", "http://audio")
    with pytest.raises(RuntimeError, match="Mount your existing token file"):
        client._settings()
