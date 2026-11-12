"""Immediate, durable AudioMuse dispatch when downloaded files become indexed in Plex."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

from app import album_verification as verify
from app import automatic_analysis as auto
from app import download_plex_sync


def file_track(key=456, album=789, suffix="01.flac"):
    return NS(
        grandparentTitle="Bill Evans", parentTitle="Waltz for Debby",
        title="Track 1", ratingKey=key, parentRatingKey=album,
        media=[NS(parts=[NS(file=
            "/plex/music/YT-DLP Imports/Bill Evans/Waltz for Debby/"+suffix)])],
    )


def download(key="download-1", *, files=None):
    return {
        "id":key,"status":"succeeded","source_title":"Waltz for Debby",
        "metadata_artist":"Bill Evans",
        "final_dir":"/data/music/YT-DLP Imports/Bill Evans/Waltz for Debby",
        "files":files or [
            "/data/music/YT-DLP Imports/Bill Evans/Waltz for Debby/01.flac"
        ],
    }


@pytest.fixture
def system(tmp_path,monkeypatch):
    monkeypatch.setattr(verify,"STATE_FILE",tmp_path/"verification.json")
    monkeypatch.setattr(auto,"STATE_FILE",tmp_path/"automatic-analysis.json")
    monkeypatch.setenv("WORKSPACE_AUTO_ANALYSIS_ENABLED","1")
    monkeypatch.setenv("WORKSPACE_AUTO_REFRESH_ENABLED","1")
    state={"jobs":[download()],"tracks":[],"scans":0,"starts":[],
           "upstream":{"active":None,"last_analysis":None}}
    class Music:
        def searchTracks(self):return list(state["tracks"])
        def update(self):state["scans"]+=1
    monkeypatch.setattr(verify,"connect",lambda:(NS(),Music()))
    monkeypatch.setattr(verify.download_audit,"read_downloader_jobs",
                        lambda:list(state["jobs"]))
    monkeypatch.setattr(verify.download_audit,"read_disk_audit",
                        lambda:[{"job_id":job["id"],"files_present":len(job["files"]),
                                 "files_missing":0} for job in state["jobs"]])
    monkeypatch.setattr(verify.download_plex_sync,"snapshot",
                        lambda:{"last_scan":None})
    monkeypatch.setattr(auto.audiomuse_analysis,"analysis_status",
                        lambda:dict(state["upstream"]))
    def start_analysis(*,recent_albums):
        state["starts"].append(recent_albums)
        return {"task_id":"task-"+str(len(state["starts"]))}
    monkeypatch.setattr(auto.audiomuse_analysis,"start_analysis",start_analysis)
    monkeypatch.setattr(verify,"_LAST_ERROR","")
    return state


def test_index_confirmation_starts_existing_analysis_without_catalogue_refresh(system):
    initial=verify.refresh(now=15000)
    assert initial["items"][0]["stage"]=="scan_requested"
    assert auto.snapshot()["baseline"] is False
    assert system["starts"]==[]
    system["tracks"]=[file_track()]
    result=verify.refresh(now=15010)
    assert result["items"][0]["stage"]=="indexed"
    assert result["items"][0]["analysis_handoff"]=="queued"
    assert result["analysis_trigger"]["started"]=="task-1"
    assert system["starts"]==[6]
    assert auto.snapshot()["inflight"]["albums"]==1
    assert auto._read()["album_tracks"]["789"]==["456"]
    assert auto._read()["verified_download_jobs"]==["download-1"]
    # No full curatorial-library refresh has been invoked.


def test_repeat_verification_and_restart_do_not_launch_duplicate_tasks(system):
    verify.refresh(now=15000)
    system["tracks"]=[file_track()]
    verify.refresh(now=15005)
    verify.refresh(now=15030)
    assert system["starts"]==[6]
    # Durable files survive worker restart and duplicate at-least-once delivery.
    admitted=auto.observe_verified_downloads(
        {"download-1":{"456":"789"}},{"456":"789"})
    assert admitted["acknowledged_jobs"]==[]
    assert admitted["queued_albums"]==0
    assert auto.snapshot()["inflight"]["task_id"]=="task-1"


def test_old_indexed_jobs_are_baselined_without_mass_reanalysis(system):
    system["tracks"]=[file_track()]
    outcome=verify.refresh(now=15000)
    assert outcome["items"][0]["stage"]=="indexed"
    assert outcome["items"][0]["analysis_handoff"]=="historical"
    assert not system["starts"]
    assert auto.snapshot()["baseline"] is False
    assert verify.refresh(now=15010)["items"][0]["analysis_handoff"]=="historical"


def test_queue_write_failure_is_retryable_and_never_marks_job_acknowledged(system,monkeypatch):
    verify.refresh(now=15000)
    system["tracks"]=[file_track()]
    real=auto.observe_verified_downloads
    def unavailable(*args,**kwargs):
        raise OSError("state storage unavailable")
    monkeypatch.setattr(auto,"observe_verified_downloads",unavailable)
    failed=verify.refresh(now=15010)["items"][0]
    assert failed["analysis_handoff"]=="pending"
    assert "storage unavailable" in failed["analysis_error"]
    assert system["starts"]==[]
    monkeypatch.setattr(auto,"observe_verified_downloads",real)
    retry=verify.refresh(now=15020)
    assert retry["items"][0]["analysis_handoff"]=="queued"
    assert retry["analysis_trigger"]["started"]=="task-1"


def test_analysis_disabled_preserves_pending_handoff_until_enabled(system,monkeypatch):
    verify.refresh(now=15000)
    system["tracks"]=[file_track()]
    monkeypatch.setenv("WORKSPACE_AUTO_ANALYSIS_ENABLED","0")
    result=verify.refresh(now=15010)
    assert result["items"][0]["analysis_handoff"]=="pending"
    assert system["starts"]==[]
    monkeypatch.setenv("WORKSPACE_AUTO_ANALYSIS_ENABLED","1")
    result=verify.refresh(now=15020)
    assert result["items"][0]["analysis_handoff"]=="queued"
    assert system["starts"]==[6]


def test_ambiguous_plex_media_path_fails_closed_and_retries(system):
    verify.refresh(now=15000)
    system["tracks"]=[file_track(456),file_track(999)]
    result=verify.refresh(now=15010)["items"][0]
    assert result["stage"]=="indexed"
    assert result["analysis_handoff"]=="pending"
    assert "unique Plex track identity" in result["analysis_error"]
    assert system["starts"]==[]


def test_two_downloads_same_album_share_one_analysis_batch(system):
    system["jobs"]=[download("first"),download("second")]
    verify.refresh(now=15000)
    system["tracks"]=[file_track()]
    result=verify.refresh(now=15010)
    assert len(result["items"])==2
    assert all(x["analysis_handoff"]=="queued" for x in result["items"])
    assert system["starts"]==[6]
    assert auto._read()["verified_download_jobs"]==["first","second"]
    assert auto.snapshot()["inflight"]["albums"]==1


def test_periodic_observer_and_immediate_handoff_share_one_durable_queue(system):
    # The usual catalogue worker discovered the track before the downloader
    # verifier. It must not generate an overlapping second analysis.
    auto.observe({"123":{"album_key":"100"}})
    auto.observe({"123":{"album_key":"100"},"456":{"album_key":"789"}})
    assert auto.snapshot()["pending"]==1
    verify.refresh(now=15000)
    system["tracks"]=[file_track()]
    result=verify.refresh(now=15010)
    assert result["items"][0]["analysis_handoff"]=="queued"
    assert system["starts"]==[6]
    assert auto.snapshot()["inflight"]["albums"]==1


def test_downloader_bridge_wakes_verifier_after_accepted_plex_scan(
    tmp_path,monkeypatch
):
    jobs=[download("old")]
    async def upstream(*args,**kwargs):return list(jobs)
    monkeypatch.setattr(download_plex_sync.downloader_bridge,"upstream_json",upstream)
    monkeypatch.setattr(download_plex_sync,"scan_plex_library",lambda:None)
    wakes=[]
    monkeypatch.setattr(verify,"request_check",lambda:wakes.append("wake"))
    path=tmp_path/"downloader-scans.json"
    asyncio.run(download_plex_sync.poll_once(state_file=path))
    assert wakes==[]  # historical baseline
    jobs.append(download("new"))
    asyncio.run(download_plex_sync.poll_once(state_file=path))
    assert wakes==["wake"]
    asyncio.run(download_plex_sync.poll_once(state_file=path))
    assert wakes==["wake"]
