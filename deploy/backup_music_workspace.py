#!/usr/bin/env python3
"""Verified, private backups of both Music workspace state volumes.

Run on the Docker host, never from an untrusted web request. No music files are
included. Downloader cookies and state tokens are sensitive; archives are mode
0600 and should remain on a private encrypted host filesystem.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import zipfile

MAX_STATE_FILE=256*1024*1024
SKIP={".lock",".tmp",".wal",".shm"}


def _sources(manager: Path, downloader: Path):
    for root,label in ((manager,"playlist-manager"),(downloader,"downloader-state")):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            if (path.suffix in SKIP or path.name.endswith(("-wal","-shm"))
                or path.name.startswith(".backup-")):
                continue
            if path.stat().st_size>MAX_STATE_FILE:
                raise RuntimeError("State file exceeds safety bound: "+str(path))
            yield label,path.relative_to(root),path


def _read_snapshot(path):
    if path.suffix not in (".sqlite",".db"):
        return path.read_bytes()
    # An online SQLite backup is transactionally consistent even while the
    # media server's background maintenance is updating feature records.
    with tempfile.NamedTemporaryFile(suffix=".sqlite",delete=False) as tmp:
        name=tmp.name
    try:
        original=sqlite3.connect(f"file:{path}?mode=ro",uri=True,timeout=15)
        copy=sqlite3.connect(name)
        try:
            original.backup(copy)
        finally:
            original.close();copy.close()
        return Path(name).read_bytes()
    finally:
        Path(name).unlink(missing_ok=True)


def verify_archive(archive: Path):
    with zipfile.ZipFile(archive) as source:
        members=source.namelist()
        if not members or members.count("manifest.json")!=1 or len(members)!=len(set(members)):
            raise RuntimeError("Incomplete or duplicate backup archive entries.")
        metadata=json.loads(source.read("manifest.json"))
        if metadata.get("format")!="music-workspace-state-v1":
            raise RuntimeError("Unsupported workspace backup format.")
        expected=set()
        for entry in metadata.get("files",[]):
            name=entry["path"]
            parts=Path(name).parts
            if (len(parts)<2 or parts[0] not in ("playlist-manager","downloader-state")
                or any(part in ("..",".") for part in parts)
                or name.startswith("/") or "\\" in name):
                raise RuntimeError("Unsafe archive filename: "+name)
            expected.add(name)
            data=source.read(name)
            if hashlib.sha256(data).hexdigest()!=entry["sha256"] or len(data)!=entry["bytes"]:
                raise RuntimeError("Backup checksum mismatch: "+name)
        if set(members)!=expected|{"manifest.json"}:
            raise RuntimeError("Backup contains unexpected or missing files.")
    return metadata


def create(manager: Path, downloader: Path, backup_dir: Path, *, keep=7,
           backup_owner_uid: int | None = None,
           backup_owner_gid: int | None = None):
    if (backup_owner_uid is None) != (backup_owner_gid is None):
        raise ValueError("Backup owner UID and GID must be provided together.")
    if backup_owner_uid is not None and (
        backup_owner_uid < 0 or backup_owner_gid < 0
    ):
        raise ValueError("Backup owner UID and GID cannot be negative.")
    backup_dir.mkdir(parents=True,exist_ok=True)
    os.chmod(backup_dir,0o700)
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination=backup_dir/("music-workspace-"+stamp+".zip")
    if destination.exists():
        raise FileExistsError("A backup for this second already exists.")
    fd,temp=tempfile.mkstemp(prefix=".music-backup-",suffix=".zip",dir=backup_dir)
    os.fchmod(fd,0o600)
    os.close(fd)
    metadata={"format":"music-workspace-state-v1",
              "created_at":datetime.now(timezone.utc).isoformat(),
              "files":[]}
    try:
        with zipfile.ZipFile(temp,"w",compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6,allowZip64=True) as archive:
            for label,relative,path in _sources(manager,downloader):
                name=label+"/"+relative.as_posix()
                data=_read_snapshot(path)
                archive.writestr(name,data)
                metadata["files"].append({
                    "path":name,"bytes":len(data),
                    "sha256":hashlib.sha256(data).hexdigest()})
            if not metadata["files"]:
                raise RuntimeError("No persistent state found; refusing an empty backup.")
            archive.writestr("manifest.json",json.dumps(metadata,indent=2))
        verify_archive(Path(temp))
        os.replace(temp,destination)
        if backup_owner_uid is not None:
            # The isolated root backup reader returns a private, mode-0600
            # archive to the ordinary deployment user without altering any
            # owner/mode on the live Docker state files.
            os.chown(destination, backup_owner_uid, backup_owner_gid)
        fd=os.open(backup_dir,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
        older=sorted(backup_dir.glob("music-workspace-*.zip"))
        for old in older[:-max(1,keep)]:
            # Only archives with a validated replacement are pruned.
            old.unlink()
        return destination
    finally:
        Path(temp).unlink(missing_ok=True)


def restore(archive: Path,manager:Path,downloader:Path,*,apply=False):
    metadata=verify_archive(archive)
    dest={"playlist-manager":manager,"downloader-state":downloader}
    plan=[]
    for entry in metadata["files"]:
        label,relative=entry["path"].split("/",1)
        target=dest[label]/relative
        if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
            raise RuntimeError("Refusing restore through symlink: "+str(target))
        plan.append(str(target))
    if not apply:
        return {"dry_run":True,"files":plan}
    with zipfile.ZipFile(archive) as source:
        for entry in metadata["files"]:
            label,relative=entry["path"].split("/",1)
            target=dest[label]/relative
            target.parent.mkdir(parents=True,exist_ok=True)
            if target.exists():
                preserved=target.with_name(target.name+".pre-restore")
                if preserved.exists():
                    raise RuntimeError("Existing pre-restore backup must be moved first: "+str(preserved))
                os.replace(target,preserved)
            fd,tmp=tempfile.mkstemp(prefix=".restore-",dir=target.parent)
            os.fchmod(fd,0o600)
            try:
                with os.fdopen(fd,"wb") as output:
                    output.write(source.read(entry["path"]))
                    output.flush();os.fsync(output.fileno())
                os.replace(tmp,target)
            finally:
                Path(tmp).unlink(missing_ok=True)
    return {"dry_run":False,"files":plan}


def require_stopped_containers():
    """Refuse a CLI restore while either service can rewrite restored state."""
    for service in ("plex-playlist-manager","yt-dlp-server"):
        try:
            result=subprocess.run(
                ["docker","inspect","--format","{{.State.Running}}",service],
                capture_output=True,text=True,timeout=8,check=False)
        except (FileNotFoundError,subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Cannot verify Docker services are stopped.") from exc
        if result.returncode == 0 and result.stdout.strip() == "true":
            raise RuntimeError(
                "Stop "+service+" before restoring persisted state. No files changed.")
        if result.returncode not in (0,1):
            raise RuntimeError("Cannot safely inspect Docker state for "+service)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manager-data",type=Path,default=Path("/srv/plex-playlist-manager"))
    parser.add_argument("--downloader-state",type=Path,default=Path("/srv/yt-dlp-server/state"))
    parser.add_argument("--backup-dir",type=Path,default=Path("/srv/music-workspace-backups"))
    parser.add_argument("--backup-owner-uid",type=int)
    parser.add_argument("--backup-owner-gid",type=int)
    parser.add_argument("--restore",type=Path)
    parser.add_argument("--apply",action="store_true",help="Explicitly overwrite with retained pre-restore copies")
    args=parser.parse_args()
    if args.restore:
        if args.apply:
            require_stopped_containers()
        print(json.dumps(restore(args.restore,args.manager_data,args.downloader_state,
                                 apply=args.apply),indent=2))
    else:
        archive=create(args.manager_data,args.downloader_state,args.backup_dir,
                       backup_owner_uid=args.backup_owner_uid,
                       backup_owner_gid=args.backup_owner_gid)
        print("Verified private backup:",archive)


if __name__=="__main__":
    main()
