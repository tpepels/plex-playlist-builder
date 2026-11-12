#!/usr/bin/env python3
"""Merge downloader and Playlist Manager settings into media-stack safely.

Only those two service blocks change. A read-only preview runs by default.
--apply validates the effective Docker Compose JSON, saves a mode-0600 backup
and atomically replaces the existing Compose file; other services are untouched.
No third-party Python modules needed on the media server.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


class MigrationError(RuntimeError):
    pass


SERVICE = re.compile(r"^  ([A-Za-z0-9_.-]+):\s*(?:#.*)?$")
FIELD = re.compile(r"^    ([A-Za-z0-9_-]+):(?:\s|$)")
TOP = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:\s*(?:#.*)?$")


def service_bounds(lines, name):
    active = False
    start = None
    for index, line in enumerate(lines):
        if TOP.match(line):
            active = line.startswith("services:")
            if start is not None and not active:
                return start, index
        if not active:
            continue
        match = SERVICE.match(line)
        if not match:
            continue
        if start is not None:
            return start, index
        if match.group(1) == name:
            start = index
    if start is None:
        raise MigrationError(f"Missing service: {name}")
    return start, len(lines)


def positions(lines):
    found = {}
    for index, line in enumerate(lines):
        match = FIELD.match(line)
        if match:
            if match.group(1) in found:
                raise MigrationError("Duplicate Compose field: " + match.group(1))
            found[match.group(1)] = index
    return found


def replace_field(lines, name, replacement):
    fields = positions(lines)
    if name not in fields:
        return lines + replacement
    start = fields[name]
    end = min((index for index in fields.values() if index > start),
              default=len(lines))
    return lines[:start] + replacement + lines[end:]


def merge_mapping(lines, name, values):
    fields = positions(lines)
    if name not in fields:
        return lines + [f"    {name}:\n"] + [
            f"      {key}: {value}\n" for key, value in values.items()
        ]
    start = fields[name]
    end = min((index for index in fields.values() if index > start),
              default=len(lines))
    block = lines[start:end]
    if any(re.match(r"^      -\s", line) for line in block[1:]):
        raise MigrationError(f"Cannot safely merge list-form {name}")
    for key, value in values.items():
        matches = [i for i, line in enumerate(block)
                   if re.match(r"^      " + re.escape(key) + r":\s", line)]
        if len(matches) > 1:
            raise MigrationError(f"Duplicate {name}.{key}")
        if matches:
            block[matches[0]] = f"      {key}: {value}\n"
        else:
            block.append(f"      {key}: {value}\n")
    return lines[:start] + block + lines[end:]


def merge_list(lines, name, values):
    fields = positions(lines)
    if name not in fields:
        return lines + [f"    {name}:\n"] + [
            f"      - {value}\n" for value in values
        ]
    start = fields[name]
    end = min((index for index in fields.values() if index > start),
              default=len(lines))
    block = lines[start:end]
    if not any(line.strip().startswith("-") for line in block[1:]):
        raise MigrationError(f"Cannot safely merge non-list {name}")
    for value in values:
        if not any(line.strip().lstrip("- ").strip('"') == value.strip('"')
                   for line in block[1:]):
            block.append(f"      - {value}\n")
    return lines[:start] + block + lines[end:]


def update_downloader_build(lines):
    """Point Docker at the imported subtree, never the old Git checkout."""
    fields = positions(lines)
    if "build" not in fields:
        raise MigrationError("yt-dlp-server has no build configuration")
    start = fields["build"]
    end = min((i for i in fields.values() if i > start), default=len(lines))
    block = lines[start:end]
    if block[0].strip().startswith("build:") and block[0].split(":", 1)[1].strip():
        value = block[0].split(":", 1)[1].strip().strip('"').strip("'")
        if value not in ("./yt-dlp-server", "./plex-playlist-manager/yt-dlp-server"):
            raise MigrationError("Unexpected yt-dlp build context; refusing to replace it")
        block = ["    build:\n", "      context: ./plex-playlist-manager/yt-dlp-server\n"]
    else:
        locations = [i for i, line in enumerate(block)
                     if line.startswith("      context:")]
        if len(locations) != 1:
            raise MigrationError("Expected one yt-dlp build.context mapping")
        i = locations[0]
        value = block[i].split(":", 1)[1].strip().strip('"').strip("'")
        if value not in ("./yt-dlp-server", "./plex-playlist-manager/yt-dlp-server"):
            raise MigrationError("Unexpected yt-dlp build context; refusing to replace it")
        block[i] = "      context: ./plex-playlist-manager/yt-dlp-server\n"
    return lines[:start] + block + lines[end:]


def downloader(lines):
    lines = update_downloader_build(lines)
    if "network_mode" in positions(lines):
        raise MigrationError("Old yt-dlp network_mode must be removed manually first")
    lines = replace_field(lines, "user", ['    user: "0:0"\n'])
    lines = replace_field(lines, "ports", [])
    lines = replace_field(lines, "expose",
                          ['    expose:\n', '      - "4545"\n'])
    lines = merge_list(lines, "cap_add", ["NET_ADMIN"])
    lines = merge_list(lines, "devices", ["/dev/net/tun:/dev/net/tun"])
    lines = merge_mapping(lines, "sysctls", {
        "net.ipv4.conf.all.src_valid_mark": '"1"',
    })
    lines = merge_mapping(lines, "environment", {
        "VPN_GATEWAY_ENABLED": '"1"',
        "VPN_PROFILE_DIR": '"/profiles"',
        "VPN_CONTROL_TOKEN_FILE": '"/data/state/.vpn-control-token"',
        "VPN_DOWNLOADER_UID": '${UID:-1000}',
        "VPN_DOWNLOADER_GID": '${GID:-1000}',
        "SURFSHARK_ROTATE_ON_403": '${SURFSHARK_ROTATE_ON_403:-1}',
    })
    return merge_list(lines, "volumes",
                      ["/srv/yt-dlp-server/wireguard-profiles:/profiles"])


def manager(lines):
    return merge_mapping(lines, "environment", {
        "YTDLP_SERVER_URL": '"http://yt-dlp-server:4545"',
        "WORKSPACE_ALLOWED_CIDRS": '"127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,fc00::/7,fe80::/10"',
        "YTDLP_PUBLIC_URL": '"/downloads"',
        "DOWNLOAD_BRIDGE_SCAN_ENABLED": '"1"',
    })


def transform(source):
    lines = source.splitlines(keepends=True)
    if "services:" not in source:
        raise MigrationError("Compose file lacks a services section")
    for name, fn in (("yt-dlp-server", downloader),
                     ("plex-playlist-manager", manager)):
        start, end = service_bounds(lines, name)
        lines = lines[:start] + fn(lines[start:end]) + lines[end:]
    return "".join(lines)


def _mount_sources(service):
    """Normalize Compose resolved bind mounts without touching data."""
    mounts = {}
    for item in service.get("volumes", []):
        if isinstance(item, dict):
            target = item.get("target")
            source = item.get("source")
            kind = item.get("type", "bind")
        elif isinstance(item, str):
            parts = item.split(":")
            if len(parts) < 2:
                continue
            source, target, kind = parts[0], parts[1], "bind"
        else:
            continue
        if target and source and kind == "bind":
            mounts[target] = str(source)
    return mounts


def _network_names(service):
    if service.get("network_mode"):
        # Host, bridge and container-network sharing would bypass the
        # project DNS namespace and break yt-dlp-server name resolution.
        raise MigrationError("Both applications must use a shared Compose network")
    networks = service.get("networks")
    if not networks:
        return {"default"}
    if isinstance(networks, dict):
        return set(networks)
    if isinstance(networks, list):
        return set(networks)
    raise MigrationError("Unsupported Compose network format")


def _build_context(service, compose):
    build = service.get("build")
    if isinstance(build, dict):
        path = build.get("context")
    elif isinstance(build, str):
        path = build
    else:
        path = None
    if not path:
        raise MigrationError("Docker build context is required")
    root = Path(compose).parent
    target = Path(path)
    return (target if target.is_absolute() else root / target).resolve()


def _has_manager_port(manager):
    for item in manager.get("ports", []):
        if isinstance(item, dict):
            if (str(item.get("published")) == "5051"
                    and str(item.get("target")) == "8000"
                    and item.get("protocol", "tcp") == "tcp"):
                return True
        elif isinstance(item, str) and item.rsplit(":", 2)[-2:] == ["5051", "8000"]:
            return True
    return False


def validate(compose, override):
    cmd = ["docker", "compose", "-f", str(compose)]
    if override.is_file():
        cmd.extend(("-f", str(override)))
    result = subprocess.run(cmd + ["config", "--format", "json"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise MigrationError("Docker Compose validation failed:\n"
                             + result.stderr[-1600:])
    try:
        config = json.loads(result.stdout)["services"]
        engine = config["yt-dlp-server"]
        pm = config["plex-playlist-manager"]
    except (KeyError, ValueError, TypeError) as exc:
        raise MigrationError("Compose lacks required resolved services") from exc
    if str(engine.get("user")) != "0:0":
        raise MigrationError("yt-dlp supervisor needs user 0:0")
    if "NET_ADMIN" not in engine.get("cap_add", []):
        raise MigrationError("yt-dlp needs NET_ADMIN")
    if not any("/dev/net/tun" in str(dev) for dev in engine.get("devices", [])):
        raise MigrationError("yt-dlp needs /dev/net/tun")
    if engine.get("ports"):
        raise MigrationError("yt-dlp still exposes port 4545 externally")
    root = Path(compose).parent
    expected = root / "plex-playlist-manager"
    if _build_context(pm, compose) != expected.resolve():
        raise MigrationError("Playlist Manager must build from the monorepo")
    if _build_context(engine, compose) != (expected / "yt-dlp-server").resolve():
        raise MigrationError("yt-dlp must build from the imported monorepo subtree")
    if not _has_manager_port(pm):
        raise MigrationError("Playlist Manager must publish port 5051 to container port 8000")
    if not (_network_names(pm) & _network_names(engine)):
        raise MigrationError("Playlist Manager and yt-dlp need a common Docker network")
    for target, source in {
        "/data/music": "/srv/completed/music",
        "/data/state": "/srv/yt-dlp-server/state",
        "/data/tmp": "/srv/yt-dlp-server/tmp",
        "/profiles": "/srv/yt-dlp-server/wireguard-profiles",
    }.items():
        actual = _mount_sources(engine).get(target)
        if actual != source:
            raise MigrationError(
                f"yt-dlp mount {target} must preserve {source}; found {actual!r}"
            )
    if _mount_sources(pm).get("/data") != "/srv/plex-playlist-manager":
        raise MigrationError("Playlist Manager must retain /srv/plex-playlist-manager:/data")
    if str(engine.get("environment", {}).get("VPN_PROFILE_DIR", "")) != "/profiles":
        raise MigrationError("VPN profiles must remain in the /profiles bind mount")
    if str(engine.get("environment", {}).get("VPN_GATEWAY_ENABLED")) != "1":
        raise MigrationError("yt-dlp VPN controller is disabled")
    if pm.get("environment", {}).get("YTDLP_SERVER_URL") != "http://yt-dlp-server:4545":
        raise MigrationError("Playlist Manager cannot reach the download engine")
    if str(pm.get("environment", {}).get("DOWNLOAD_BRIDGE_SCAN_ENABLED")) != "1":
        raise MigrationError("Plex download scan polling is not enabled")
    return True


def migrate(stack, apply):
    root = Path(stack).expanduser().resolve()
    found = [p for p in (root / "docker-compose.yml", root / "compose.yaml")
             if p.is_file()]
    if len(found) != 1:
        raise MigrationError("Expected exactly one docker-compose.yml or compose.yaml")
    compose = found[0]
    override = root / "docker-compose.override.yml"
    old_target = (root / "yt-dlp-server" / "compose.parent-vpn.yaml").resolve()
    new_target = (root / "plex-playlist-manager" / "yt-dlp-server"
                  / "compose.parent-vpn.yaml").resolve()
    repoint = override.is_symlink() and override.resolve() == old_target
    if repoint and not new_target.is_file():
        raise MigrationError(
            "Imported VPN override is missing; pull the merged repository first"
        )
    original_link = os.readlink(override) if repoint else None
    original = compose.read_text(encoding="utf-8")
    updated = transform(original)
    if updated == original and not repoint:
        validate(compose, override)
        print("Already merged. The effective configuration is valid.")
        return None

    fd, name = tempfile.mkstemp(prefix=".download-merge-", suffix=".yml", dir=root)
    tmp = Path(name)
    candidate_override = None
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as writer:
            writer.write(updated)
            writer.flush()
            os.fsync(writer.fileno())
        if repoint:
            candidate_override = root / (".download-merge-vpn-" + os.urandom(6).hex() + ".yml")
            os.symlink(new_target, candidate_override)
        validate(tmp, candidate_override if repoint else override)
        if not apply:
            print("Dry run passed: one Git checkout, both services and effective VPN override.")
            return None

        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = root / f"{compose.name}.pre-monorepo.{stamp}.bak"
        if backup.exists():
            raise MigrationError(f"Backup already exists: {backup}")
        shutil.copy2(compose, backup)
        backup.chmod(0o600)
        os.chmod(tmp, compose.stat().st_mode & 0o777)
        os.replace(tmp, compose)
        if repoint:
            os.replace(candidate_override, override)
            candidate_override = None
        try:
            validate(compose, override)
        except MigrationError:
            shutil.copy2(backup, compose)
            if repoint:
                recovery_link = root / (".restore-vpn-" + os.urandom(6).hex())
                os.symlink(original_link, recovery_link)
                os.replace(recovery_link, override)
            raise
        print(f"Compose validated. Private backup: {backup.name}")
        if repoint:
            print("WireGuard override repointed into the monorepo.")
        return backup
    finally:
        tmp.unlink(missing_ok=True)
        if candidate_override is not None:
            candidate_override.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", type=Path,
                        default=Path(__file__).resolve().parents[2])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        migrate(args.stack, args.apply)
        return 0
    except (MigrationError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
