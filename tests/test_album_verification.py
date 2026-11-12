"""Real file-path evidence, bounded scan retries and durable import checkpoints."""
from types import SimpleNamespace as NS
import json

import pytest
from fastapi.testclient import TestClient

from app import album_verification as verify
from app import main


def job(key="album-1",status="succeeded"):
    return {"id":key,"status":status,"source_title":"Waltz for Debby",
            "metadata_artist":"Bill Evans",
            "final_dir":"/data/music/YT-DLP Imports/Bill Evans/Waltz for Debby",
            "files":["/data/music/YT-DLP Imports/Bill Evans/Waltz for Debby/01.flac"]}


def indexed():
    return [NS(grandparentTitle="Bill Evans",parentTitle="Waltz for Debby",
               title="Track 1",ratingKey=456,media=[NS(parts=[NS(
                   file="/plex/music/YT-DLP Imports/Bill Evans/Waltz for Debby/01.flac"
               )])])]


@pytest.fixture
def server(tmp_path,monkeypatch):
    monkeypatch.setattr(verify,"STATE_FILE",tmp_path/"verification.json")
    state={"jobs":[job()],"disk":[{"job_id":"album-1","files_present":1,"files_missing":0}],
           "tracks":[],"scans":0,"recent_scan":None}
    class Music:
        def searchTracks(self):return state["tracks"]
        def update(self):state["scans"]+=1
    monkeypatch.setattr(verify.download_audit,"read_downloader_jobs",lambda:state["jobs"])
    monkeypatch.setattr(verify.download_audit,"read_disk_audit",lambda:state["disk"])
    monkeypatch.setattr(verify,"connect",lambda:(None,Music()))
    monkeypatch.setattr(verify.download_plex_sync,"snapshot",
                        lambda:{"last_scan":state["recent_scan"]})
    return state


def test_completed_download_remains_unverified_until_paths_exist_in_plex(server):
    first=verify.refresh(now=50000)
    assert first["counts"]["scan_requested"]==1
    assert server["scans"]==1
    assert first["items"][0]["indexed_files"]==0
    assert verify.snapshot()["items"][0]["stage"]=="scan_requested"
    verify.refresh(now=50050)
    assert server["scans"]==1
    server["tracks"]=indexed()
    complete=verify.refresh(now=50100)
    assert complete["counts"]=={"indexed":1}
    assert complete["items"][0]["indexed_files"]==1
    assert complete["items"][0]["indexed_at"]
    assert verify.snapshot()["items"][0]["attempts"]==1


def test_many_albums_get_one_scan_request_not_a_scan_storm(server):
    next_job=job("album-2")
    server["jobs"].append(next_job)
    server["disk"].append({"job_id":"album-2","files_present":1,"files_missing":0})
    report=verify.refresh(now=100000)
    assert report["counts"]["scan_requested"]==2
    assert server["scans"]==1


def test_missing_disk_files_never_trigger_plex_scan(server):
    server["disk"]=[{"job_id":"album-1","files_present":0,"files_missing":1}]
    report=verify.refresh(now=100000)
    assert report["items"][0]["stage"]=="missing_on_disk"
    assert server["scans"]==0


def test_partial_publication_does_not_claim_entire_download_completed(server):
    server["jobs"]=[job(status="partial")]
    server["tracks"]=indexed()
    report=verify.refresh(now=100000)
    assert report["items"][0]["stage"]=="partially_downloaded"
    assert server["scans"]==0


def test_rescan_retries_are_bounded_and_status_requires_attention(server):
    for i in range(4):
        report=verify.refresh(now=20000+i*700)
    assert server["scans"]==3
    assert report["items"][0]["stage"]=="requires_attention"
    assert "mounts" in report["items"][0]["reason"]
    assert verify.snapshot()["items"][0]["attempts"]==3


def test_scan_failure_does_not_claim_indexing(server,monkeypatch):
    def failure():raise OSError("Plex offline")
    class BrokenMusic:
        def searchTracks(self):return []
        def update(self):failure()
    monkeypatch.setattr(verify,"connect",lambda:(None,BrokenMusic()))
    report=verify.refresh(now=15000)
    assert report["items"][0]["stage"]=="scan_failed"
    assert report["items"][0]["attempts"]==1
    assert not report["items"][0]["indexed_at"]


def test_corrupt_state_is_not_overwritten(server):
    verify.STATE_FILE.write_text("{corrupt",encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        verify.refresh(now=1000)
    assert verify.STATE_FILE.read_text()=="{corrupt"
    assert verify.snapshot()["ok"] is False


def test_read_only_api_exposes_verified_stage(server,monkeypatch):
    verify.refresh(now=100000)
    server["tracks"]=indexed()
    verify.refresh(now=100100)
    with TestClient(main.app) as client:
        result=client.get("/api/album-verification")
        assert result.status_code==200
        assert result.json()["items"][0]["stage"]=="indexed"
    assert server["scans"]==1
