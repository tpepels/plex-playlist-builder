"""End-to-end private backup verification and explicit restore semantics."""
import importlib.util
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

MODULE=Path(__file__).resolve().parents[1]/"deploy"/"backup_music_workspace.py"
spec=importlib.util.spec_from_file_location("workspace_backup",MODULE)
backup=importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def fixtures(tmp_path):
    manager=tmp_path/"manager"
    downloader=tmp_path/"downloader"
    manager.mkdir();downloader.mkdir()
    (manager/"playlists.yml").write_text("playlists: {Jazz: {items: [{rating_key: 1}]}}\n")
    (downloader/"jobs.json").write_text('{"jobs":["done"]}')
    db=sqlite3.connect(manager/"audiomuse-cache.sqlite")
    db.execute("CREATE TABLE tracks(id TEXT,score REAL)")
    db.execute("INSERT INTO tracks VALUES('1',0.5)")
    db.commit();db.close()
    (downloader/"link").symlink_to(manager/"playlists.yml")
    return manager,downloader


def test_verified_backup_includes_both_stores_and_consistent_sqlite(tmp_path):
    manager,downloader=fixtures(tmp_path)
    archive=backup.create(manager,downloader,tmp_path/"backups")
    assert archive.stat().st_mode & 0o777 == 0o600
    assert (tmp_path/"backups").stat().st_mode & 0o777 == 0o700
    metadata=backup.verify_archive(archive)
    names={f["path"] for f in metadata["files"]}
    assert {"playlist-manager/playlists.yml",
            "playlist-manager/audiomuse-cache.sqlite",
            "downloader-state/jobs.json"} <= names
    assert "downloader-state/link" not in names
    with zipfile.ZipFile(archive) as source:
        restored_db=tmp_path/"from-backup.sqlite"
        restored_db.write_bytes(source.read("playlist-manager/audiomuse-cache.sqlite"))
    db=sqlite3.connect(restored_db)
    assert db.execute("SELECT score FROM tracks WHERE id='1'").fetchone()==(.5,)
    db.close()


def test_restore_is_dry_run_by_default_and_keeps_original_revisions(tmp_path):
    manager,downloader=fixtures(tmp_path)
    archive=backup.create(manager,downloader,tmp_path/"backups")
    (manager/"playlists.yml").write_text("changed")
    (downloader/"jobs.json").write_text("changed")
    dry=backup.restore(archive,manager,downloader)
    assert dry["dry_run"] is True
    assert (manager/"playlists.yml").read_text()=="changed"
    result=backup.restore(archive,manager,downloader,apply=True)
    assert result["dry_run"] is False
    assert (manager/"playlists.yml").read_text().startswith("playlists:")
    assert (manager/"playlists.yml.pre-restore").read_text()=="changed"
    assert json.loads((downloader/"jobs.json").read_text())=={"jobs":["done"]}
    with pytest.raises(RuntimeError,match="pre-restore"):
        backup.restore(archive,manager,downloader,apply=True)


def test_tampered_or_traversal_archive_is_rejected_before_restore(tmp_path):
    manager,downloader=fixtures(tmp_path)
    archive=backup.create(manager,downloader,tmp_path/"backups")
    tampered=tmp_path/"tampered.zip"
    with zipfile.ZipFile(archive) as original,zipfile.ZipFile(tampered,"w") as changed:
        for item in original.namelist():
            data=original.read(item)
            if item=="downloader-state/jobs.json":data=b"malicious"
            changed.writestr(item,data)
    with pytest.raises(RuntimeError,match="checksum"):
        backup.verify_archive(tampered)
    with pytest.raises(RuntimeError,match="checksum"):
        backup.restore(tampered,manager,downloader,apply=True)
