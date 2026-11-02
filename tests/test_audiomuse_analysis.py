"""Fast offline checks of AudioMuse analysis task controls."""
import pytest
from app import audiomuse_analysis as control


def test_status_maps_active_and_last_analysis(monkeypatch):
    def fake_call(path, **kwargs):
        if path == "/api/active_tasks":
            return {
                "task_id": "abc", "task_type": "main_analysis", "status": "PROGRESS",
                "progress": 37, "details": {"current_album": "A Love Supreme"}
            }
        if path == "/api/last_task":
            return {
                "task_id": "old", "task_type": "main_analysis",
                "status": "SUCCESS", "progress": 100
            }
        pytest.fail(path)
    monkeypatch.setattr(control, "_call", fake_call)
    status = control.analysis_status()
    assert status["busy"] is True
    assert status["active"]["progress"] == 37
    assert status["active"]["message"] == "A Love Supreme"
    assert status["last_analysis"]["state"] == "SUCCESS"


def test_no_active_task(monkeypatch):
    def fake_call(path, **kwargs):
        if path == "/api/active_tasks":
            return {}
        return {"task_id": "abc", "task_type": "main_clustering", "status": "SUCCESS"}
    monkeypatch.setattr(control, "_call", fake_call)
    status = control.analysis_status()
    assert not status["busy"]
    assert status["active"] is None
    assert status["last_analysis"] is None


def test_full_library_requires_confirmation(monkeypatch):
    monkeypatch.setattr(control, "_call", lambda *args, **kwargs: pytest.fail("Do not send"))
    with pytest.raises(ValueError, match="explicit confirmation"):
        control.start_analysis(recent_albums=0)


def test_rejects_invalid_bounds(monkeypatch):
    monkeypatch.setattr(control, "_call", lambda *args, **kwargs: pytest.fail("Do not send"))
    for value in (-1, 1001, True, 10.5):
        with pytest.raises(ValueError):
            control.start_analysis(recent_albums=value)


def test_start_recent_and_full_library(monkeypatch):
    requests = []
    def fake_call(path, *, post=None):
        requests.append((path, post))
        return {"task_id": "task-one", "status": "queued"}
    monkeypatch.setattr(control, "_call", fake_call)
    recent = control.start_analysis(recent_albums=25)
    full = control.start_analysis(recent_albums=0, confirm_full_library=True)
    assert recent["full_library"] is False
    assert full["full_library"] is True
    assert requests == [
        ("/api/analysis/start", {"num_recent_albums": 25}),
        ("/api/analysis/start", {"num_recent_albums": 0}),
    ]


def test_start_no_task_id_rejected(monkeypatch):
    monkeypatch.setattr(control, "_call", lambda *args, **kwargs: {"status": "queued"})
    with pytest.raises(control.AudioMuseControlError, match="task ID"):
        control.start_analysis(recent_albums=25)


def test_sanitized_long_status_details():
    result = control._summary({
        "task_id": "example", "task_type": "main_analysis", "state": "PROGRESS",
        "progress": 999, "details": {"message": "A" * 500}
    })
    assert result["progress"] == 100
    assert len(result["message"]) == 180
    assert "details" not in result
