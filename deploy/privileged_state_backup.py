#!/usr/bin/env python3
"""Backup root-owned Docker state without changing permissions on live files.

Use the Python runtime of the *currently installed* playlist-manager image,
as root in a one-off container with no network, read-only source mounts and a
single writable backup directory. The actual verified archive is returned to
the host user with 0600 permissions. Fail closed before Compose or containers
are changed if any stage fails.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

from backup_music_workspace import verify_archive


class BackupPreflightError(RuntimeError):
    pass


IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


def _mount(source: Path, target: str, *, readonly: bool = False) -> str:
    # Docker --mount separates fields with commas. Refuse an ambiguous source
    # rather than accidentally binding an unintended path.
    source_text = str(source)
    if any(mark in source_text for mark in (",", "\n", "\r")):
        raise BackupPreflightError("Invalid character in Docker bind source.")
    return "type=bind,source=" + source_text + ",target=" + target + (
        ",readonly" if readonly else ""
    )


def backup(stack: Path, manager_checkout: Path, *,
           manager_data: Path = Path("/srv/plex-playlist-manager"),
           downloader_state: Path = Path("/srv/yt-dlp-server/state"),
           backup_dir: Path | None = None) -> Path:
    stack = Path(stack).resolve(strict=True)
    manager_checkout = Path(manager_checkout).resolve(strict=True)
    backup_script = manager_checkout / "deploy" / "backup_music_workspace.py"
    if not backup_script.is_file():
        raise BackupPreflightError("Missing verified workspace backup implementation.")
    for location in (manager_data, downloader_state):
        # The host user may not even have search permission on a parent of
        # the Docker-owned state directory. Do not call is_dir()/resolve()
        # here: Docker's --mount (not -v) checks existence as the daemon,
        # and will fail rather than create a missing bind-source directory.
        if not location.is_absolute():
            raise BackupPreflightError("Persistent state mounts must be absolute paths.")

    output = backup_dir if backup_dir is not None else stack / "private-state-backups"
    if not output.is_absolute():
        raise BackupPreflightError("Private backup output must be an absolute path.")
    if output.is_symlink():
        raise BackupPreflightError("Refusing a symlinked private backup directory.")
    output.mkdir(mode=0o700, exist_ok=True)
    if output.is_symlink() or not output.is_dir() or output.stat().st_uid != os.getuid():
        raise BackupPreflightError("Private backup directory must belong to the deploy user.")
    output.chmod(0o700)

    # The old image is already present and known to work with this server.
    # Using its digest does not pull an image or rebuild/interrupt services.
    try:
        inspected = subprocess.run(
            ["docker", "container", "inspect", "--format", "{{.Image}}",
             "plex-playlist-manager"],
            check=True, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise BackupPreflightError(
            "Cannot inspect the existing playlist-manager container image; "
            "no persistent state was modified."
        ) from exc
    image = inspected.stdout.strip()
    if not IMAGE_ID.fullmatch(image):
        raise BackupPreflightError("Docker returned an unexpected image ID.")

    previous = {p.name for p in output.glob("music-workspace-*.zip")}
    docker_cmd = [
        "docker", "run", "--rm", "--network", "none", "--read-only",
        "--user", "0:0", "--security-opt", "no-new-privileges",
        "--cap-drop", "ALL", "--cap-add", "DAC_READ_SEARCH",
        "--cap-add", "FOWNER", "--cap-add", "CHOWN",
        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=536870912",
        "--mount", _mount(backup_script.parent, "/backup-tools", readonly=True),
        "--mount", _mount(manager_data, "/backup-manager", readonly=True),
        "--mount", _mount(downloader_state, "/backup-downloader", readonly=True),
        "--mount", _mount(output, "/backup-output"),
        "--entrypoint", "python", image,
        "/backup-tools/backup_music_workspace.py",
        "--manager-data", "/backup-manager",
        "--downloader-state", "/backup-downloader",
        "--backup-dir", "/backup-output",
        "--backup-owner-uid", str(os.getuid()),
        "--backup-owner-gid", str(os.getgid()),
    ]
    try:
        finished = subprocess.run(
            docker_cmd, capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise BackupPreflightError(
            "The isolated Docker state backup failed: " + detail.strip()[-1400:]
        ) from exc
    if finished.stdout:
        print(finished.stdout.strip())

    new = [p for p in output.glob("music-workspace-*.zip")
           if p.name not in previous]
    if len(new) != 1:
        raise BackupPreflightError(
            "Could not identify one newly created private state archive."
        )
    archive = new[0]
    if archive.is_symlink() or not archive.is_file():
        raise BackupPreflightError("Backup archive path is unsafe.")
    info = archive.stat()
    if info.st_uid != os.getuid() or info.st_gid != os.getgid():
        raise BackupPreflightError("Backup archive is not owned by the deploy user.")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise BackupPreflightError("Private backup archive has incorrect permissions.")
    manifest = verify_archive(archive)
    components = {entry["path"].split("/")[0] for entry in manifest["files"]}
    if components != {"playlist-manager", "downloader-state"}:
        raise BackupPreflightError("Backup is missing one persistent state volume.")
    print("Verified both private state volumes in:", archive)
    return archive


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", type=Path, required=True)
    parser.add_argument("--manager", type=Path, required=True)
    parser.add_argument("--manager-data", type=Path,
                        default=Path("/srv/plex-playlist-manager"))
    parser.add_argument("--downloader-state", type=Path,
                        default=Path("/srv/yt-dlp-server/state"))
    parser.add_argument("--backup-dir", type=Path,
                        help="An existing/private directory owned by the deploy user")
    args = parser.parse_args(argv)
    try:
        backup(args.stack, args.manager,
               manager_data=args.manager_data,
               downloader_state=args.downloader_state,
               backup_dir=args.backup_dir)
    except (BackupPreflightError, OSError, ValueError) as exc:
        print("ERROR:", exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
