"""A file selection cannot persist YAML or accidentally sync prior pending work."""
from fastapi.testclient import TestClient

from app import config_store, main


def test_import_requires_exact_revision_bound_review_and_explicit_approval(tmp_path,monkeypatch):
    monkeypatch.setattr(config_store,"PLAYLISTS_FILE",tmp_path/"playlists.yml")
    monkeypatch.setattr(config_store,"STATE_FILE",tmp_path/"playlist-state.yml")
    config_store.PLAYLISTS_FILE.write_text(
        "playlists:\n  Alpha:\n    items:\n      - rating_key: 1\n",encoding="utf-8")
    incoming="playlists:\n  Beta:\n    items:\n      - rating_key: 2\n"
    with TestClient(main.app) as client:
        review=client.post("/api/inspect-config",json={"text":incoming})
        assert review.status_code==200
        original=config_store.PLAYLISTS_FILE.read_text()
        preview=review.json()
        assert preview["new"]==["Beta"]
        assert preview["changes"][0]["name"]=="Beta"
        assert original==config_store.PLAYLISTS_FILE.read_text()
        for body in (
            {"text":incoming,"filename":"test.yml","approved":False,
             "review_token":preview["review_token"]},
            {"text":incoming,"filename":"test.yml","approved":True,
             "review_token":"wrong"},
        ):
            denied=client.post("/api/import-config",json=body)
            assert denied.status_code==409
            assert config_store.PLAYLISTS_FILE.read_text()==original
        saved=client.post("/api/import-config",json={
            "text":incoming,"filename":"test.yml","approved":True,
            "review_token":preview["review_token"]})
        assert saved.status_code==200,saved.text
        assert saved.json()["changed"]==["Beta"]
        assert set(config_store.load()["playlists"])=={"Alpha","Beta"}
        assert client.post("/api/import-config",json={
            "text":incoming,"filename":"test.yml","approved":True,
            "review_token":preview["review_token"]}).status_code==409


def test_outdated_yaml_preview_cannot_overwrite_new_changes(tmp_path,monkeypatch):
    monkeypatch.setattr(config_store,"PLAYLISTS_FILE",tmp_path/"playlists.yml")
    monkeypatch.setattr(config_store,"STATE_FILE",tmp_path/"playlist-state.yml")
    config_store.PLAYLISTS_FILE.write_text(
        "playlists:\n  A:\n    items:\n      - rating_key: 1\n",encoding="utf-8")
    draft="playlists:\n  B:\n    items:\n      - rating_key: 2\n"
    with TestClient(main.app) as client:
        preview=client.post("/api/inspect-config",json={"text":draft}).json()
        config_store.save_text("playlists:\n  A:\n    items:\n      - rating_key: 3\n")
        response=client.post("/api/import-config",json={
            "text":draft,"approved":True,"review_token":preview["review_token"]})
        assert response.status_code==409
        assert set(config_store.load()["playlists"])=={"A"}
