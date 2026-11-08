"""Monorepo invariants: both ancestries, two services and one Git checkout."""
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOWNLOADER = ROOT / "yt-dlp-server"
SOURCE_HEAD_AT_IMPORT = "f0daaabbcf605e3c54592d2df36f8a96ff515914"


def test_complete_downloader_tree_is_in_version_control():
    for name in ("Dockerfile", "app.py", "container_runtime.py",
                 "vpn_gateway.py", "youtube_cookies.py", "requirements.txt",
                 "tests/test_download_resilience.py", "templates/index.html",
                 "chrome-extension/manifest.json"):
        assert (DOWNLOADER / name).is_file(), name
    assert not (DOWNLOADER / ".git").exists()


def test_both_git_histories_remain_reachable():
    # Checkout in CI uses fetch-depth: 0. The subtree merge has two parents,
    # one preserving the complete source repository history unchanged.
    result = subprocess.run(
        ["git", "cat-file", "-t", SOURCE_HEAD_AT_IMPORT],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "commit"
    commits = subprocess.run(
        ["git", "rev-list", "--all"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert SOURCE_HEAD_AT_IMPORT in commits


def test_root_compose_builds_both_apps_from_one_checkout():
    config = yaml.safe_load((ROOT / "compose.yaml").read_text())
    services = config["services"]
    assert set(services) == {"plex-playlist-manager", "yt-dlp-server"}
    manager = services["plex-playlist-manager"]
    downloader = services["yt-dlp-server"]
    assert manager["build"]["context"] == "."
    assert downloader["build"]["context"] == "./yt-dlp-server"
    assert manager["ports"] == ["5051:8000"]
    assert "ports" not in downloader
    assert downloader["expose"] == ["4545"]
    assert downloader["user"] == "0:0"
    assert "NET_ADMIN" in downloader["cap_add"]
    assert downloader["sysctls"]["net.ipv4.conf.all.src_valid_mark"] == "1"
    assert any("/dev/net/tun" in x for x in downloader["devices"])
    assert any("/srv/completed/music:/data/music" in x
               for x in downloader["volumes"])
    assert manager["environment"]["YTDLP_SERVER_URL"] == "http://yt-dlp-server:4545"
    assert manager["environment"]["DOWNLOAD_BRIDGE_SCAN_ENABLED"] == "1"


def test_manager_image_does_not_embed_downloader_image_build_context():
    ignored = (ROOT / ".dockerignore").read_text().splitlines()
    assert "yt-dlp-server/" in ignored
    docker = (ROOT / "Dockerfile").read_text()
    assert "COPY app ./app" in docker
    engine = (DOWNLOADER / "Dockerfile").read_text()
    assert "container_runtime.py" in engine
    assert "wireguard-tools" in engine


def test_shared_ci_and_no_legacy_action_workflows():
    checks = (ROOT / ".github/workflows/checks.yml").read_text()
    docker = (ROOT / ".github/workflows/downloader-docker.yml").read_text()
    assert "downloader-tests:" in checks
    assert "working-directory: yt-dlp-server" in checks
    assert "python -m pytest -q tests" in checks
    assert "working-directory: yt-dlp-server" in docker
    assert not (ROOT / ".github/workflows/monorepo-import.yml").exists()
    assert not (DOWNLOADER / ".github/workflows/fast-checks.yml").exists()
    assert not (DOWNLOADER / ".github/workflows/docker.yml").exists()


def test_migration_no_longer_needs_old_standalone_git_checkout():
    wrapper = (ROOT / "deploy/apply_media_stack.sh").read_text()
    migrator = (ROOT / "deploy/merge_media_stack.py").read_text()
    assert 'ENGINE="$MANAGER/yt-dlp-server"' in wrapper
    assert 'git -C "$ENGINE" pull' not in wrapper
    assert 'git -C "$MANAGER" pull --ff-only' in wrapper
    assert "./plex-playlist-manager/yt-dlp-server" in migrator
    assert "compose.parent-vpn.yaml" in migrator
