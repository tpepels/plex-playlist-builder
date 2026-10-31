"""Fast offline checks for the separate AudioMuse deployment image references."""
import re
from pathlib import Path


ROOT = Path(__file__).parents[1] / "deploy" / "audiomuse"


def test_upstream_release_tag_uses_ghcr_numeric_form():
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    match = re.search(r"^AUDIOMUSE_TAG=(.+)$", env, re.MULTILINE)
    assert match, "Deployment sample requires an explicit image version"
    tag = match.group(1)
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", tag), (
        "Upstream GHCR image tags do not have the git-release 'v' prefix"
    )
    assert compose.count("AUDIOMUSE_TAG:-" + tag) == 2


def test_database_port_not_published():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    postgres = compose.split("  postgres:", 1)[1].split("  audiomuse-ai-flask:", 1)[0]
    assert "ports:" not in postgres
