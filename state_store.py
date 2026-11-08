"""Recoverable, atomic JSON state shared by downloader and WireGuard.

Primary files are never replaced by an empty default after corruption.
A validated previous generation is retained as <name>.bak. Recovery moves
the damaged primary aside before restoring the backup, preserving forensic
evidence and all valid user data. Every durable write fsyncs the file and
parent directory. No network calls or logging of state contents.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path


class StateCorruption(RuntimeError):
    """Both durable generations are missing/invalid where state was expected."""


def _check(value, validate):
    if validate is not None and not validate(value):
        raise ValueError("State schema is not valid")
    return value


def _sync_directory(folder):
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_payload(path, encoded):
    fd, name = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, value, *, validate=None, previous_valid=True):
    """Write a new generation while keeping one *validated* older generation.

    Does not create a backup from a known-corrupt primary. Callers serialize
    concurrent updates using their existing jobs/gateway locks.
    """
    path = Path(path)
    _check(value, validate)
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".bak")
    if previous_valid and path.exists():
        try:
            _check(json.loads(path.read_text(encoding="utf-8")), validate)
        except (OSError, ValueError, UnicodeError, TypeError):
            raise StateCorruption(
                f"{path.name} is damaged; refusing to overwrite its last available copy"
            ) from None
        # Copy-on-write via hard link: atomic rename later preserves this
        # previous inode even if the primary changes. Never remove a valid
        # backup until the candidate old primary has been checked.
        link = path.with_name("." + path.name + ".backup-" + uuid.uuid4().hex)
        try:
            os.link(path, link)
            os.replace(link, backup)
            _sync_directory(path.parent)
        finally:
            link.unlink(missing_ok=True)
    _write_payload(path, encoded)


def load_json(path, *, default, validate=None):
    """Read state, recovering a valid backup; never silently reset corruption."""
    path = Path(path)
    backup = path.with_name(path.name + ".bak")
    if not path.exists() and not backup.exists():
        return default, False

    for candidate in (path, backup):
        if not candidate.exists():
            continue
        try:
            value = _check(json.loads(candidate.read_text(encoding="utf-8")), validate)
        except (OSError, ValueError, UnicodeError, TypeError):
            continue
        if candidate == path:
            return value, False

        if path.exists():
            quarantine = path.with_name(
                path.name + ".corrupt-" + uuid.uuid4().hex[:12]
            )
            os.replace(path, quarantine)
            _sync_directory(path.parent)
        # Retain backup while restoring primary, so a crash between rename
        # and fsync is recoverable at the next startup.
        _write_payload(path, json.dumps(
            value, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8"))
        return value, True

    raise StateCorruption(
        f"{path.name} and its backup are unreadable or invalid; "
        "original files were preserved. Restore a known-good state backup."
    )


def validate_jobs_payload(data):
    if isinstance(data, list):
        return all(isinstance(x, dict) and isinstance(x.get("id"), str) for x in data)
    if not isinstance(data, dict):
        return False
    jobs = data.get("jobs")
    archived = data.get("archived_jobs", [])
    claims = data.get("album_submissions", {})
    return (isinstance(jobs, list)
            and isinstance(archived, list)
            and isinstance(claims, dict)
            and all(isinstance(x, dict) and isinstance(x.get("id"), str)
                    for x in jobs + archived))


def validate_profiles_payload(data):
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), list):
        return False
    profiles = data["profiles"]
    return (len(profiles) <= 20
            and (data.get("active") is None or isinstance(data["active"], str))
            and all(isinstance(entry, dict)
                    and isinstance(entry.get("id"), str)
                    and isinstance(entry.get("filename"), str)
                    and isinstance(entry.get("country"), str)
                    for entry in profiles))
