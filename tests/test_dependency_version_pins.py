"""Short static checks of the downloader's reproducible stable tool pins."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def test_bgutil_python_plugin_and_server_script_versions_match():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    plugin = re.search(r"^bgutil-ytdlp-pot-provider==(\d+\.\d+\.\d+)$", requirements, re.M)
    server = re.search(r"^ARG BGUTIL_VERSION=(\d+\.\d+\.\d+)$", dockerfile, re.M)
    assert plugin is not None, "BgUtils plugin must be pinned to a stable release"
    assert server is not None, "BgUtils server scripts must be pinned to a stable release"
    assert plugin.group(1) == server.group(1), "BgUtils plugin and script versions must match"


def test_spotdl_uses_a_stable_release_and_yt_dlp_follows_pypi():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"^spotdl==\d+\.\d+\.\d+$", requirements, re.M)
    assert re.search(r"^yt-dlp\[default,curl-cffi\]$", requirements, re.M)
    assert "FROM python:3.13-slim" in (ROOT / "Dockerfile").read_text(encoding="utf-8")
