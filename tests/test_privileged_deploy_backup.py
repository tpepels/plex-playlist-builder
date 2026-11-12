"""Regression: Docker-owned 0600 state must not block the host deployment.

This tests the exact Docker mount/privilege contract without needing a daemon
in CI. Both live mounts must be read-only. No chmod/chown on source data is
permitted, and a complete, owner-private, checksummed archive is mandatory.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
sys.path.insert(0, str(DEPLOY))
try:
    import backup_music_workspace as archive_tool
    import privileged_state_backup as runner
finally:
    sys.path.pop(0)


def data(tmp_path):
    stack = tmp_path / "media-stack"
    manager = stack / "plex-playlist-manager"
    deploy = manager / "deploy"
    deploy.mkdir(parents=True)
    (deploy / "backup_music_workspace.py").write_text("# backup script")
    state = tmp_path / "srv" / "plex-playlist-manager"
    downloader = tmp_path / "srv" / "yt-dlp-server" / "state"
    state.mkdir(parents=True)
    downloader.mkdir(parents=True)
    (state / "album-verification.json").write_bytes(b'{"version":1,"items":{}}')
    (state / "workspace-analysis.json").write_bytes(b'{"pending_albums":[]}')
    (downloader / "jobs.json").write_bytes(b'{"jobs":[]}')
    (downloader / ".vpn-control-token").write_bytes(b"private token")
    (state / "album-verification.json").chmod(0o600)
    (downloader / ".vpn-control-token").chmod(0o600)
    return stack, manager, state, downloader


def test_root_owned_state_is_backed_up_in_readonly_isolated_container(
    tmp_path, monkeypatch
):
    stack,manager,state,downloader=data(tmp_path)
    seen = []
    def fake_docker(args, **kwargs):
        seen.append(args)
        if args[:3] == ["docker", "container", "inspect"]:
            return subprocess.CompletedProcess(args, 0, "sha256:" + "c"*64, "")
        if args[:2] == ["docker", "run"]:
            archive = archive_tool.create(
                state, downloader, stack / "private-state-backups",
            )
            return subprocess.CompletedProcess(
                args, 0, "Verified private backup: " + str(archive), "",
            )
        pytest.fail("Unexpected Docker command: " + repr(args))
    monkeypatch.setattr(runner.subprocess, "run", fake_docker)
    original = {
        p: (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
        for p in (state / "album-verification.json",
                  state / "workspace-analysis.json",
                  downloader / "jobs.json",
                  downloader / ".vpn-control-token")
    }

    result = runner.backup(stack, manager, manager_data=state,
                           downloader_state=downloader)
    assert result.is_file()
    assert runner.verify_archive(result)["format"] == "music-workspace-state-v1"
    assert stat.S_IMODE(result.stat().st_mode) == 0o600
    assert result.stat().st_uid == os.getuid()
    assert result.stat().st_gid == os.getgid()
    assert {
        p: (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
        for p in original
    } == original

    commands = [x for x in seen if x[:2] == ["docker", "run"]]
    assert len(commands) == 1
    cmd = commands[0]
    assert cmd[:2] == ["docker", "run"]
    for flag in ("--rm","--network","--read-only","--user",
                 "--cap-drop","--security-opt"):
        assert flag in cmd
    assert cmd[cmd.index("--network")+1] == "none"
    assert cmd[cmd.index("--user")+1] == "0:0"
    assert cmd[cmd.index("--cap-drop")+1] == "ALL"
    for capability in ("DAC_READ_SEARCH","FOWNER","CHOWN"):
        assert capability in [
            cmd[i+1] for i,v in enumerate(cmd[:-1]) if v == "--cap-add"
        ]
    mounts = [cmd[i+1] for i,v in enumerate(cmd[:-1]) if v == "--mount"]
    assert len(mounts) == 4
    for source in (manager / "deploy", state, downloader):
        matches = [x for x in mounts if "source="+str(source)+"," in x]
        assert len(matches) == 1 and matches[0].endswith(",readonly")
    assert any("target=/backup-output" in x and
               not x.endswith(",readonly") for x in mounts)
    assert cmd[cmd.index("--entrypoint")+1] == "python"
    assert cmd[-4:] == [
        "--backup-owner-uid",str(os.getuid()),
        "--backup-owner-gid",str(os.getgid())]
    assert cmd.count("sha256:"+"c"*64) == 1


def test_docker_permission_failure_blocks_deployment_without_mutating_state(
    tmp_path,monkeypatch
):
    stack,manager,state,downloader=data(tmp_path)
    original=(state / "album-verification.json").read_bytes()
    def unavailable(args,**kwargs):
        if args[:3] == ["docker", "container", "inspect"]:
            return subprocess.CompletedProcess(args,0,"sha256:"+"a"*64,"")
        raise subprocess.CalledProcessError(
            1,args,stderr="Cannot read source volume")
    monkeypatch.setattr(runner.subprocess,"run",unavailable)
    with pytest.raises(runner.BackupPreflightError,match="Cannot read source volume"):
        runner.backup(stack,manager,manager_data=state,
                      downloader_state=downloader)
    assert not list((stack / "private-state-backups").glob("*.zip"))
    assert (state / "album-verification.json").read_bytes()==original


def test_missing_previous_image_stops_before_any_backup_or_compose_mutation(
    tmp_path,monkeypatch
):
    stack,manager,state,downloader=data(tmp_path)
    calls=[]
    def missing(args,**kwargs):
        calls.append(args)
        raise subprocess.CalledProcessError(1,args,stderr="No such container")
    monkeypatch.setattr(runner.subprocess,"run",missing)
    with pytest.raises(runner.BackupPreflightError,match="Cannot inspect"):
        runner.backup(stack,manager,manager_data=state,
                      downloader_state=downloader)
    assert len(calls)==1
    assert calls[0][:3]==["docker","container","inspect"]


def test_partial_backup_cannot_satisfy_complete_deploy_preflight(
    tmp_path,monkeypatch
):
    stack,manager,state,downloader=data(tmp_path)
    def fake_docker(args,**kwargs):
        if args[:3]==["docker","container","inspect"]:
            return subprocess.CompletedProcess(args,0,"sha256:"+"c"*64,"")
        empty=tmp_path/"empty-downloader"
        empty.mkdir(exist_ok=True)
        archive_tool.create(state,empty,stack/"private-state-backups")
        return subprocess.CompletedProcess(args,0,"", "")
    monkeypatch.setattr(runner.subprocess,"run",fake_docker)
    with pytest.raises(runner.BackupPreflightError,match="missing one persistent"):
        runner.backup(stack,manager,manager_data=state,
                      downloader_state=downloader)


def test_backup_reader_sets_requested_archive_ownership(tmp_path,monkeypatch):
    stack,manager,state,downloader=data(tmp_path)
    output=stack/"archive"
    calls=[]
    monkeypatch.setattr(archive_tool.os,"chown",
                        lambda path,uid,gid:calls.append((Path(path),uid,gid)))
    path=archive_tool.create(
        state,downloader,output,
        backup_owner_uid=os.getuid(),backup_owner_gid=os.getgid())
    assert calls==[(path,os.getuid(),os.getgid())]
    assert stat.S_IMODE(path.stat().st_mode)==0o600
    assert archive_tool.verify_archive(path)["files"]


def test_actual_deployment_calls_privileged_reader_not_host_user_backup():
    script=(DEPLOY/"apply_media_stack.sh").read_text()
    backup_section=script.split(
        'step "Verified private backup of both persistent state volumes"',
        1)[1].split('step "Validate proposed Compose build paths',1)[0]
    assert 'python3 "$MANAGER/deploy/privileged_state_backup.py"' in backup_section
    assert 'python3 "$MANAGER/deploy/backup_music_workspace.py"' not in backup_section
    assert 'error "Persistent state backup failed; refusing to rebuild containers"' in backup_section
