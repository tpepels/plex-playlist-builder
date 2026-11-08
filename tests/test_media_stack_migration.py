"""One-command media-stack integration: narrow, reversible, idempotent edits."""
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

from deploy import merge_media_stack as migration


ORIGINAL = """services:
  monochrome:
    image: example/unrelated:latest
    restart: unless-stopped

  yt-dlp-server:
    build:
      context: ./yt-dlp-server
    container_name: yt-dlp-server
    user: "${UID}:${GID}"
    ports:
      - "4545:4545"
    environment:
      TZ: ${TZ}
      PORT: 4545
      MUSIC_ROOT: /data/music
      STATE_DIR: /data/state
      TEMP_DIR: /data/tmp
    volumes:
      - /srv/completed/music:/data/music
      - /srv/yt-dlp-server/state:/data/state
      - /srv/yt-dlp-server/tmp:/data/tmp
    restart: unless-stopped

  plex-playlist-manager:
    build:
      context: ./plex-playlist-manager
    container_name: plex-playlist-manager
    ports:
      - "5051:8000"
    environment:
      TZ: ${TZ}
      PLEX_URL: https://host.docker.internal:32400
      PLEX_LIBRARY: Music
      PLAYLISTS_FILE: /data/playlists.yml
    volumes:
      - /srv/plex-playlist-manager:/data
    restart: unless-stopped

  unrelated-finance:
    image: example/finance:latest
    environment:
      LOCAL_TEST_KEY: sample-only
    restart: unless-stopped
"""


def service(source, name):
    lines = source.splitlines(keepends=True)
    a, b = migration.service_bounds(lines, name)
    return "".join(lines[a:b])


def test_only_download_and_manager_services_are_edited():
    merged = migration.transform(ORIGINAL)
    for name in ("monochrome", "unrelated-finance"):
        assert service(merged, name) == service(ORIGINAL, name)
    assert "4545:4545" not in service(merged, "yt-dlp-server")
    assert "context: ./plex-playlist-manager/yt-dlp-server" in service(
        merged, "yt-dlp-server")
    assert '      - "4545"' in service(merged, "yt-dlp-server")
    assert 'user: "0:0"' in merged
    assert "NET_ADMIN" in merged
    assert "/dev/net/tun:/dev/net/tun" in merged
    assert "VPN_GATEWAY_ENABLED" in merged
    assert "YTDLP_SERVER_URL" in merged
    assert 'YTDLP_PUBLIC_URL: "/downloads"' in merged
    assert 'DOWNLOAD_BRIDGE_SCAN_ENABLED: "1"' in merged
    assert "5051:8000" in merged
    assert "4545:4545" not in merged


def test_migration_is_idempotent_and_valid_yaml():
    once = migration.transform(ORIGINAL)
    twice = migration.transform(once)
    assert once == twice
    data = yaml.safe_load(once)["services"]
    assert set(data) == {
        "monochrome", "yt-dlp-server", "plex-playlist-manager",
        "unrelated-finance",
    }
    assert data["yt-dlp-server"]["user"] == "0:0"
    assert "ports" not in data["yt-dlp-server"]
    assert "NET_ADMIN" in data["yt-dlp-server"]["cap_add"]
    assert data["yt-dlp-server"]["build"]["context"] == (
        "./plex-playlist-manager/yt-dlp-server"
    )
    assert data["plex-playlist-manager"]["environment"]["YTDLP_SERVER_URL"] == (
        "http://yt-dlp-server:4545"
    )


def fake_compose(monkeypatch):
    commands = []
    def command(argv, **kwargs):
        commands.append(argv)
        assert argv[:3] == ["docker", "compose", "-f"]
        original = yaml.safe_load(Path(argv[3]).read_text())["services"]
        engine = original["yt-dlp-server"]
        # docker compose config resolves the build context to an absolute
        # path relative to the first Compose file.
        engine["build"]["context"] = str(
            (Path(argv[3]).parent / engine["build"]["context"]).resolve()
        )
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"services": original}), stderr=""
        )
    monkeypatch.setattr(migration.subprocess, "run", command)
    return commands


def test_dry_run_has_zero_filesystem_side_effects(tmp_path, monkeypatch):
    stack = tmp_path / "media-stack"
    stack.mkdir()
    source = stack / "docker-compose.yml"
    source.write_text(ORIGINAL)
    commands = fake_compose(monkeypatch)
    assert migration.migrate(stack, apply=False) is None
    assert source.read_text() == ORIGINAL
    assert not list(stack.glob("*.bak"))
    assert commands


def test_apply_retains_private_backup_and_verifies_both_services(tmp_path, monkeypatch):
    stack = tmp_path / "media-stack"
    stack.mkdir()
    source = stack / "docker-compose.yml"
    source.write_text(ORIGINAL)
    override = stack / "docker-compose.override.yml"
    override.write_text("services:\n  unrelated:\n    image: test\n")
    original_override = override.read_text()
    fake_compose(monkeypatch)
    backup = migration.migrate(stack, apply=True)
    assert backup.is_file()
    assert backup.read_text() == ORIGINAL
    assert backup.stat().st_mode & 0o777 == 0o600
    assert source.read_text() == migration.transform(ORIGINAL)
    assert override.read_text() == original_override
    assert migration.migrate(stack, apply=True) is None
    assert len(list(stack.glob("*.bak"))) == 1


def test_bad_compose_does_not_modify_original_or_create_backup(tmp_path, monkeypatch):
    stack = tmp_path
    original = stack / "compose.yaml"
    original.write_text(ORIGINAL)
    monkeypatch.setattr(migration.subprocess, "run", lambda *args, **kwargs:
        subprocess.CompletedProcess(args, 1, stdout="", stderr="invalid Compose"))
    with pytest.raises(migration.MigrationError, match="invalid Compose"):
        migration.migrate(stack, apply=True)
    assert original.read_text() == ORIGINAL
    assert not list(stack.glob("*.bak"))


def test_legacy_gateway_networking_is_rejected_not_silently_bypassed():
    source = ORIGINAL.replace(
        '    user: "${UID}:${GID}"',
        '    user: "${UID}:${GID}"\n    network_mode: "service:wireguard-gateway"',
    )
    with pytest.raises(migration.MigrationError, match="network_mode"):
        migration.transform(source)


def test_wrapper_only_recreates_two_containers_and_never_removes_volumes():
    root = Path(__file__).resolve().parents[1]
    shell = root / "deploy" / "apply_media_stack.sh"
    result = subprocess.run(["bash", "-n", str(shell)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    content = shell.read_text()
    assert "docker compose build yt-dlp-server plex-playlist-manager" in content
    assert "docker compose up -d --no-deps --no-build --force-recreate yt-dlp-server" in content
    assert "docker compose up -d --no-deps --no-build --force-recreate plex-playlist-manager" in content
    assert "docker compose down" not in content
    assert "rm -rf" not in content
    assert 'git -C "$MANAGER" pull --ff-only' in content
    assert 'git -C "$ENGINE" pull' not in content
    assert "Preflight: refuse migration" in content
    assert 'job.get("status") in {"queued", "running", "finalizing"}' in content
    assert "Bridge health check failed" in content


def test_invalid_service_file_does_not_create_a_replacement():
    with pytest.raises(migration.MigrationError, match="Missing service"):
        migration.transform("services:\n  only-unrelated:\n    image: alpine\n")


def test_existing_vpn_override_symlink_is_repointed_with_parent_compose(
    tmp_path, monkeypatch,
):
    stack = tmp_path / "media-stack"
    stack.mkdir()
    compose = stack / "docker-compose.yml"
    compose.write_text(ORIGINAL)
    old = stack / "yt-dlp-server" / "compose.parent-vpn.yaml"
    old.parent.mkdir()
    old.write_text("services:\n  yt-dlp-server:\n    user: '0:0'\n")
    new = stack / "plex-playlist-manager" / "yt-dlp-server" / "compose.parent-vpn.yaml"
    new.parent.mkdir(parents=True)
    new.write_text("services:\n  yt-dlp-server:\n    user: '0:0'\n")
    override = stack / "docker-compose.override.yml"
    override.symlink_to(old)
    fake_compose(monkeypatch)
    assert migration.migrate(stack, apply=False) is None
    assert override.resolve() == old.resolve()
    backup = migration.migrate(stack, apply=True)
    assert backup.is_file()
    assert override.is_symlink()
    assert override.resolve() == new.resolve()
    assert "plex-playlist-manager/yt-dlp-server" in compose.read_text()
    assert migration.migrate(stack, apply=True) is None


def test_monorepo_migration_refuses_to_repoint_to_nonexistent_override(
    tmp_path,
):
    stack = tmp_path / "media-stack"
    stack.mkdir()
    (stack / "compose.yaml").write_text(ORIGINAL)
    override = stack / "docker-compose.override.yml"
    override.symlink_to(stack / "yt-dlp-server" / "compose.parent-vpn.yaml")
    with pytest.raises(migration.MigrationError, match="VPN override is missing"):
        migration.migrate(stack, apply=False)
    assert override.is_symlink()


def test_custom_unexpected_build_context_requires_manual_review():
    custom = ORIGINAL.replace(
        "context: ./yt-dlp-server",
        "context: /opt/my-special-download-image",
    )
    with pytest.raises(migration.MigrationError, match="Unexpected yt-dlp build"):
        migration.transform(custom)


def test_one_checkout_path_survives_repeated_migrations():
    one = migration.transform(ORIGINAL)
    two = migration.transform(one)
    assert one == two
    assert "context: ./yt-dlp-server" not in two
    assert "context: ./plex-playlist-manager/yt-dlp-server" in two
