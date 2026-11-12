"""Persisted automatic analysis lifecycle, including restarts and task failures."""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from app import automatic_analysis as auto


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(auto, "STATE_FILE", tmp_path / "analysis.json")
    monkeypatch.setenv("WORKSPACE_AUTO_ANALYSIS_ENABLED", "1")
    calls = []
    upstream = {"active": None, "last_analysis": None}
    monkeypatch.setattr(auto.audiomuse_analysis, "analysis_status", lambda: dict(upstream))
    def start_analysis(*, recent_albums):
        calls.append(recent_albums)
        return {"task_id": "task-" + str(len(calls))}
    monkeypatch.setattr(auto.audiomuse_analysis, "start_analysis", start_analysis)
    monkeypatch.setattr(auto.audiomuse_client, "synchronize", lambda: {"pending":0})
    import base64,struct
    packed=base64.b64encode(struct.pack("<4f",.1,.2,.3,.4)).decode()
    def features():
        state=auto._read() or {}
        keys={key for values in (state.get("album_tracks") or {}).values()
              for key in values}
        return {key: {"embedding":packed,"clap_embedding":packed}
                for key in keys}, {"pending":0}
    monkeypatch.setattr(auto.audiomuse_client, "cached_features", features)
    return calls, upstream


def library(*pairs):
    return {track: {"album_key": album} for track, album in pairs}


def test_initial_scan_baselines_without_analyzing_existing_music(state):
    calls, _ = state
    assert auto.observe(library(("1", "A"), ("2", "A")))["baseline"]
    assert auto.run_once()["pending"] == 0
    assert calls == []
    assert auto.snapshot()["baseline"]


def test_new_album_and_new_track_in_existing_album_trigger_one_batch(state):
    calls, upstream = state
    auto.observe(library(("1", "A")))
    observed = auto.observe(library(("1", "A"), ("2", "B"), ("3", "A")))
    assert observed["new_tracks"] == 2
    assert observed["new_albums"] == 2
    assert auto.snapshot()["pending"] == 2
    launched = auto.run_once()
    assert launched["albums"] == 2
    assert calls == [7]
    assert auto.run_once()["uncertain"]
    upstream["active"] = {"task_id": "task-1", "task_type": "main_analysis"}
    assert auto.run_once()["running"]
    upstream["active"] = None
    upstream["last_analysis"] = {"task_id": "task-1", "state": "completed"}
    assert auto.run_once()["completed"]
    assert auto.snapshot()["pending"] == 0
    assert auto.snapshot()["inflight"] is None
    assert len(calls) == 1
    # Restart reads durable state; no duplicate enqueuing.
    assert auto.observe(library(("1", "A"), ("2", "B"), ("3", "A")))["new_tracks"] == 0
    assert auto.run_once()["pending"] == 0


def test_new_music_during_running_task_is_queued_separately(state):
    calls, upstream = state
    auto.observe(library(("1", "A")))
    auto.observe(library(("1", "A"), ("2", "B")))
    auto.run_once()
    auto.observe(library(("1", "A"), ("2", "B"), ("3", "C")))
    upstream["active"] = {"task_id": "task-1"}
    assert auto.run_once()["pending"] == 1
    upstream["active"] = None
    upstream["last_analysis"] = {"task_id": "task-1", "state": "finished"}
    assert auto.run_once()["albums"] == 1
    assert calls == [6, 6]
    assert auto.snapshot()["inflight"]["task_id"] == "task-2"


def test_external_analysis_does_not_allow_concurrent_submission(state):
    calls, upstream = state
    auto.observe(library(("1", "A")))
    auto.observe(library(("1", "A"), ("2", "B")))
    upstream["active"] = {"task_id": "external", "task_type": "main_analysis"}
    assert auto.run_once()["busy"]
    assert calls == []
    upstream["active"] = None
    auto.run_once()
    assert calls == [6]


def test_failed_job_retries_bounded_and_state_persists(state):
    calls, upstream = state
    auto.observe(library(("1", "A")))
    auto.observe(library(("1", "A"), ("2", "B")))
    auto.run_once()
    for i in (1, 2):
        upstream["last_analysis"] = {"task_id": f"task-{i}", "state": "failed"}
        assert auto.run_once()["retry"]
        assert auto.run_once()["started"] == f"task-{i+1}"
    upstream["last_analysis"] = {"task_id": "task-3", "state": "failed"}
    assert auto.run_once()["blocked"] == 1
    assert "manual review" in auto.snapshot()["error"]
    assert len(calls) == 3


def test_error_or_unknown_task_never_silently_discards_queue(state, monkeypatch):
    calls, upstream = state
    auto.observe(library(("1", "A")))
    auto.observe(library(("1", "A"), ("2", "B")))
    auto.run_once()
    upstream["last_analysis"] = {"task_id": "other-task", "state": "completed"}
    assert auto.run_once()["uncertain"]
    assert auto.snapshot()["inflight"]["task_id"] == "task-1"
    def broken():
        raise RuntimeError("Offline")
    monkeypatch.setattr(auto.audiomuse_analysis, "analysis_status", broken)
    with pytest.raises(RuntimeError, match="Offline"):
        auto.run_once()
    assert "Cannot verify" in auto.snapshot()["error"]


def test_invalid_persisted_state_fails_closed(tmp_path, state):
    auto.STATE_FILE.write_text("{broken", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        auto.snapshot()
    with pytest.raises(json.JSONDecodeError):
        auto.observe(library(("7", "X")))


def test_disabled_automation_does_not_write_state(state, monkeypatch):
    monkeypatch.setenv("WORKSPACE_AUTO_ANALYSIS_ENABLED", "0")
    assert auto.observe(library(("1", "A")))["disabled"]
    assert auto.run_once()["disabled"]
    assert not auto.STATE_FILE.exists()


def test_parallel_checks_do_not_submit_same_batch_twice(state):
    calls, _ = state
    auto.observe(library(("1", "A")))
    auto.observe(library(("1", "A"), ("2", "B")))
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: auto.run_once(), range(3)))
    assert sum("started" in result for result in results) == 1
    assert calls == [6]


def test_finished_task_with_missing_embeddings_is_not_marked_complete(state, monkeypatch):
    calls,upstream=state
    auto.observe(library(("1","A")))
    auto.observe(library(("1","A"),("2","B")))
    auto.run_once()
    monkeypatch.setattr(auto.audiomuse_client,"cached_features",
                        lambda: ({"2":{"embedding":"missing","clap_embedding":None}},
                                 {"pending":0}))
    upstream["last_analysis"]={"task_id":"task-1","state":"completed"}
    result=auto.run_once()
    assert result["coverage_incomplete"]==1 and result["pending"]==1
    snap=auto.snapshot()
    assert snap["last_completed"] is None
    assert snap["last_coverage"]["missing_track_ids"] == {"B":["2"]}
    assert "lack complete" in snap["error"]
    assert auto.run_once()["started"]=="task-2"


def test_coverage_waits_until_feature_cache_is_synchronized(state,monkeypatch):
    calls,upstream=state
    auto.observe(library(("1","A")))
    auto.observe(library(("1","A"),("2","B")))
    auto.run_once()
    upstream["last_analysis"]={"task_id":"task-1","state":"completed"}
    monkeypatch.setattr(auto.audiomuse_client,"synchronize",lambda:{"pending":10})
    result=auto.run_once()
    assert result["verifying"]
    assert auto.snapshot()["inflight"]["task_id"]=="task-1"
    assert auto.snapshot()["last_completed"] is None


def test_repeated_task_success_without_target_coverage_blocks_after_three_tries(state,monkeypatch):
    calls,upstream=state
    auto.observe(library(("1","A")))
    auto.observe(library(("1","A"),("2","B")))
    monkeypatch.setattr(auto.audiomuse_client,"cached_features",
                        lambda: ({},{"pending":0}))
    for i in range(1,4):
        assert auto.run_once()["started"]==f"task-{i}"
        upstream["last_analysis"]={"task_id":f"task-{i}","state":"completed"}
        res=auto.run_once()
        assert res["coverage_incomplete"]==1
    assert res["blocked"]==1
    assert auto.snapshot()["pending"]==0
    assert auto.snapshot()["incomplete_albums"]==["B"]
    assert "Manual review" in auto.snapshot()["error"]
