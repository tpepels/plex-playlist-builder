"""Fast regressions for the stable, persistent album-submission contract."""
from __future__ import annotations

import json
import queue

import pytest
from album_submission import canonical_album_id, clean_match_audit, submission_key


URL = "https://music.youtube.com/playlist?list=OLAK5uy_testAlbum"


def test_canonical_identity_ignores_extraneous_playlist_query_parameters():
    assert canonical_album_id(URL + "&feature=share") == "playlist:OLAK5uy_testAlbum"
    assert submission_key(URL) == submission_key(URL + "&feature=share")
    assert len(submission_key(URL)) == 64
    assert canonical_album_id("https://music.youtube.com/search?q=album") is None
    assert canonical_album_id("https://evil.example/playlist?list=OLAK5uy_bad") is None


def test_match_audit_bounded_and_deterministic():
    audit = clean_match_audit({
        "requested_artist": "Miles Davis", "matched_album": "Blackhawk, Complete",
        "normalization_rules": ["case_punctuation", "edition_suffix"],
        "queries": ["Miles Davis Blackhawk"] * 30,
        "untrusted": {"nested": ["x" * 100000]},
    })
    assert audit["matched_album"] == "Blackhawk, Complete"
    assert len(audit["queries"]) == 15
    assert "untrusted" not in audit


@pytest.fixture
def server(tmp_path, monkeypatch):
    import app as service
    monkeypatch.setattr(service, "JOBS_STATE_FILE", tmp_path / "jobs.json")
    monkeypatch.setattr(service, "download_queue", queue.Queue(maxsize=10))
    with service.jobs_lock:
        original_jobs = dict(service.jobs)
        original_claims = dict(service.album_submissions)
        service.jobs.clear()
        service.album_submissions.clear()
    yield service
    with service.jobs_lock:
        service.jobs.clear()
        service.jobs.update(original_jobs)
        service.album_submissions.clear()
        service.album_submissions.update(original_claims)


def test_api_creates_one_job_and_returns_same_id_after_repeated_requests(server):
    client = server.app.test_client()
    body = {"url": URL, "match_audit": {
        "requested_artist": "Miles Davis", "requested_album": "Complete",
        "matched_album": "At the Blackhawk, Complete", "match_type": "edition",
        "normalization_rules": ["edition_suffix"],
    }}
    first = client.post("/api/albums", json=body)
    assert first.status_code == 201
    response = first.get_json()
    assert response["ok"] and response["status"] == "queued"
    assert response["duplicate"] is False
    job_id = response["job_id"]
    assert server.jobs[job_id]["match_audit"]["match_type"] == "edition"

    second = client.post("/api/albums", json=body)
    assert second.status_code == 200
    assert second.get_json()["duplicate"] is True
    assert second.get_json()["job_id"] == job_id
    assert server.download_queue.qsize() == 1

    detail = client.get(f"/api/albums/{job_id}")
    assert detail.status_code == 200
    assert detail.get_json()["match_audit"]["requested_artist"] == "Miles Davis"

    persisted = json.loads(server.JOBS_STATE_FILE.read_text(encoding="utf-8"))
    assert persisted["album_submissions"][submission_key(URL)]["job_id"] == job_id


def test_claim_survives_restart_and_pruned_history(server):
    client = server.app.test_client()
    job_id = client.post("/api/albums", json={"url": URL}).get_json()["job_id"]
    with server.jobs_lock:
        server.jobs.clear()  # Historical job was pruned from the UI.
    duplicate = client.post("/api/albums", json={"url": URL})
    assert duplicate.status_code == 200
    assert duplicate.get_json()["job_id"] == job_id
    assert duplicate.get_json()["status"] == "queued"  # persisted last-known status
    assert server.download_queue.qsize() == 1

    # Simulate state restoration: the submission key survives pruning.
    with server.jobs_lock:
        server.album_submissions.clear()
    server.load_persisted_jobs()
    after_restart = client.post("/api/albums", json={"url": URL})
    assert after_restart.status_code == 200
    assert after_restart.get_json()["job_id"] == job_id


def test_bad_album_url_never_enqueues(server):
    client = server.app.test_client()
    response = client.post("/api/albums", json={
        "url": "https://music.youtube.com/search?q=Miles+Davis",
    })
    assert response.status_code == 400
    assert server.download_queue.empty()


def test_full_queue_does_not_create_a_job_or_claim(server):
    server.download_queue = queue.Queue(maxsize=1)
    server.download_queue.put_nowait("other")
    response = server.app.test_client().post("/api/albums", json={"url": URL})
    assert response.status_code == 429
    assert not server.album_submissions


def test_html_album_submission_and_json_api_share_identity(server, monkeypatch):
    monkeypatch.setattr(server, "probe_url", lambda url: {
        "kind": "playlist", "title": "Test Album",
        "count": 4, "album_artist": "Artist", "thumbnail": None,
    })
    client = server.app.test_client()
    first = client.post("/download", data={"url": URL, "playlist": "on"})
    assert first.status_code == 302
    original = next(iter(server.jobs))
    again = client.post("/download", data={"url": URL, "playlist": "on"})
    assert again.status_code == 302
    assert server.download_queue.qsize() == 1
    api = client.post("/api/albums", json={"url": URL})
    assert api.status_code == 200
    assert api.get_json()["duplicate"] is True
    assert api.get_json()["job_id"] == original
