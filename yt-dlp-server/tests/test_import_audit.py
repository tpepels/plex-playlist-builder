"""Fast real-filesystem diagnostics without Plex credentials."""
from pathlib import Path

import app as service


def test_import_audit_distinguishes_missing_published_files(tmp_path, monkeypatch):
    folder = tmp_path / "YT-DLP Imports" / "Pianist" / "Beethoven"
    folder.mkdir(parents=True)
    present = folder / "01 Allegro.opus"
    absent = folder / "02 Largo.opus"
    present.write_bytes(b"fake")
    monkeypatch.setattr(service, "public_jobs", lambda: [
        {"id": "beethoven-1", "request_member_ids": ["beethoven-1"],
         "status": "succeeded", "final_dir": str(folder),
         "files": [str(present), str(absent)],
         "staging_owner_id": "beethoven-1"},
    ])
    monkeypatch.setattr(service, "TEMP_DIR", tmp_path / "staging-root")
    with service.app.app_context():
        records = service.api_import_audit().get_json()
    assert len(records) == 1
    row = records[0]
    assert row["files_recorded"] == 2
    assert row["files_present"] == 1
    assert row["files_missing"] == 1
    assert row["missing_paths"] == [str(absent)]
    assert row["final_dir_exists"] is True
    assert row["staged_audio_count"] == 0


def test_unpublished_staging_is_reported_not_claimed_as_library_file(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "TEMP_DIR", tmp_path)
    staging = tmp_path / "staging" / "beethoven-stage"
    staging.mkdir(parents=True)
    (staging / "mov-01.opus").write_bytes(b"not published")
    monkeypatch.setattr(service, "public_jobs", lambda: [
        {"id": "beethoven-stage", "request_member_ids": ["beethoven-stage"],
         "status": "needs_metadata", "final_dir": "",
         "files": [], "staging_owner_id": "beethoven-stage"},
    ])
    with service.app.app_context():
        row = service.api_import_audit().get_json()[0]
    assert row["final_dir_exists"] is False
    assert row["stage_dir_exists"] is True
    assert row["staged_audio_count"] == 1
