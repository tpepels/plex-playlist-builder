#!/usr/bin/env bash
# Migrate only Playlist Manager and yt-dlp. Preserve persistent volumes,
# other services and the original Compose file in a private backup.
set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
STACK="$(cd "$SCRIPT_DIR/../.." && pwd)"
MANAGER="$STACK/plex-playlist-manager"
ENGINE="$MANAGER/yt-dlp-server"

error() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
step() { printf '\n==> %s\n' "$*"; }

[ -d "$MANAGER/.git" ] || error "Missing Playlist Manager checkout: $MANAGER"
[ -f "$ENGINE/Dockerfile" ] || error "Missing imported yt-dlp subtree: $ENGINE"
command -v python3 >/dev/null 2>&1 || error "Python 3 is required"
command -v docker >/dev/null 2>&1 || error "Docker is required"
docker compose version >/dev/null 2>&1 || error "Docker Compose plugin required"
[ -c /dev/net/tun ] || error "Missing /dev/net/tun for WireGuard"

# The active job queue is durable. Recreating the engine while it downloads
# would unnecessarily interrupt it. Fail BEFORE pulling/building/editing.
require_idle_downloader() {
engine_running="$(docker container inspect --format '{{.State.Running}}' yt-dlp-server 2>/dev/null || true)"
if [ "$engine_running" = "true" ]; then
  if ! docker exec -i yt-dlp-server python - <<'PY'
import json
import sys
from urllib.request import urlopen
try:
    with urlopen("http://127.0.0.1:4545/api/jobs", timeout=8) as response:
        jobs = json.load(response)
    if not isinstance(jobs, list):
        raise ValueError("Expected an array of download jobs")
    active = [
        job for job in jobs if isinstance(job, dict) and (
            job.get("status") in {"queued", "running", "finalizing"}
            or job.get("active_resolution_tracks")
        )
    ]
except Exception as exc:
    print(f"Cannot verify downloader idle state: {type(exc).__name__}", file=sys.stderr)
    sys.exit(1)
print(f"Active/queued downloads: {len(active)}")
sys.exit(1 if active else 0)
PY
  then
    error "Could not confirm an idle downloader; nothing was changed. Check port 4545 and the job queue."
  fi
fi
}

step "Preflight: refuse migration while downloads are active or queued"
require_idle_downloader

step "Fast-forward the single monorepo checkout"
git -C "$MANAGER" pull --ff-only ||
    error "Monorepo git pull failed; no Compose changes made"
[ -f "$ENGINE/Dockerfile" ] ||
    error "Monorepo checkout is missing yt-dlp-server after pull"

# A stale JavaScript lookup can abort the entire playlist workspace before
# either Plex health or YAML loading runs. Check the rendered HTML contract
# before touching Compose or replacing any container.
step "Verify playlist interface controls match their JavaScript references"
python3 - "$MANAGER/app/templates/index.html" <<'PY'
import re
import sys
from pathlib import Path

page = Path(sys.argv[1]).read_text(encoding="utf-8")
if page.count("<script>") != 1:
    raise SystemExit("Unexpected playlist bootstrap script count")
script = page.split("<script>", 1)[1].split("</script>", 1)[0]
ids = set(re.findall(r'\bid="([^"]+)"', page))
lookups = set(re.findall(r'\$\("([^"]+)"\)', script))
missing = sorted(lookups - ids)
if missing:
    raise SystemExit("Playlist bootstrap references missing HTML controls: " + ", ".join(missing))
print("Playlist bootstrap DOM references: OK")
PY
if [ "$?" -ne 0 ]; then
    error "Playlist JavaScript would fail initialization; no container changes made"
fi

step "Validate proposed Compose build paths and VPN override"
python3 "$MANAGER/deploy/merge_media_stack.py" --stack "$STACK" ||
    error "Dry run failed; containers were not changed"

step "Apply Compose changes, retaining a private backup"
python3 "$MANAGER/deploy/merge_media_stack.py" --stack "$STACK" --apply ||
    error "Compose migration failed; containers were not changed"

cd "$STACK" || error "Could not enter media-stack"
docker compose config --quiet || error "Merged Compose configuration invalid"

step "Build both services without stopping the existing containers"
docker compose build yt-dlp-server plex-playlist-manager ||
    error "Build failed. Existing containers are unchanged. Private Compose backup retained."

step "Recheck the queue immediately before replacing containers"
require_idle_downloader

step "Recreate only the two download services"
if docker container inspect yt-dlp-wireguard-gateway >/dev/null 2>&1; then
    docker rm -f yt-dlp-wireguard-gateway ||
        error "Failed to remove the obsolete gateway container"
fi
docker compose up -d --no-deps --no-build --force-recreate yt-dlp-server ||
    error "yt-dlp deployment failed. Inspect container logs."

# Do not recreate the web app while its backend is stuck starting or unhealthy.
# Dockerfile's healthcheck verifies both WireGuard control and the download API.
step "Verify downloader is healthy before replacing Playlist Manager"
ready=0
for check in $(seq 1 35); do
  health="$(docker container inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' yt-dlp-server 2>/dev/null || true)"
  if [ "$health" = "healthy" ]; then
    ready=1
    break
  fi
  if [ "$health" = "missing" ]; then
    error "Downloader has no Docker healthcheck. Playlist Manager was left running."
  fi
  sleep 2
done
[ "$ready" = "1" ] ||
  error "Downloader is not healthy. Playlist Manager was left running. Inspect: docker compose logs --tail=100 yt-dlp-server"

docker compose up -d --no-deps --no-build --force-recreate plex-playlist-manager ||
    error "Playlist Manager deployment failed. Inspect container logs."

step "Check the updated services"
docker compose ps yt-dlp-server plex-playlist-manager ||
    error "Could not list service status"

step "Verify the home, playlists and native downloads interface on port 5051"
python3 - <<'PY'
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

base = "http://127.0.0.1:5051"
checks = (
    ("/", b'id="siteNavigationPanel"'),
    ("/playlists", b'id="viewPlaylists"'),
    ("/playlist-generator", b'id="genre"'),
    ("/downloads", b'id="download-form"'),
)
for attempt in range(15):
    try:
        for path, marker in checks:
            with urlopen(base + path, timeout=5) as response:
                page = response.read(1_500_000)
            if marker not in page:
                raise ValueError("Expected UI component missing from " + path)
            if page.count(b'id="siteNavigationPanel"') != 1 or page.count(b'id="workspaceTopbar"') != 1:
                raise ValueError("Shared navigation is missing or duplicated on " + path)
            if path == "/" and (b'id="home-playlist-file"' not in page
                                or b'id="home-download-form"' not in page):
                raise ValueError("Home has no working YAML or download entry point")
            if path == "/playlist-generator" and (
                b'id="subgenre"' not in page or
                b'id="referenceSearchResults"' not in page or
                b'id="generatorReviewDialog"' not in page or
                b'id="publishApproval"' not in page):
                raise ValueError("Genre-aware generation and review are missing")
            if path == "/downloads" and b"<iframe" in page.lower():
                raise ValueError("Downloads still contains a nested iframe")
        with urlopen(base + "/api/config", timeout=8) as response:
            config = json.load(response)
        if not isinstance(config.get("definitions"), dict):
            raise ValueError("Saved playlist configuration is not available")
        if not isinstance(config.get("version", {}).get("counts", {}).get("total"), int):
            raise ValueError("Playlist count is missing from configuration")
        with urlopen(base + "/api/downloads/bridge/status", timeout=5) as response:
            state = json.load(response)
        if not state.get("ok"):
            raise ValueError("Download engine is not reachable through Playlist Manager")
        print("Home, Playlists, genre generator, native Downloads and downloader bridge: OK")
        sys.exit(0)
    except (OSError, ValueError, HTTPError, URLError) as exc:
        if attempt == 14:
            print("Verification failed: " + str(exc), file=sys.stderr)
            print("Inspect: docker compose logs --tail=100 yt-dlp-server plex-playlist-manager",
                  file=sys.stderr)
            break
    time.sleep(2)
sys.exit(1)
PY
if [ "$?" -ne 0 ]; then
  error "Deployment did not pass the home/playlist/download verification. Preserve the Compose backup."
fi

printf '\nHome: http://<media-server>:5051/\n'
printf 'Playlists: http://<media-server>:5051/playlists\n'
printf 'Create genre playlist: http://<media-server>:5051/playlist-generator\n'
printf 'Downloads: http://<media-server>:5051/downloads\n'
printf 'Existing Plex playlists are not changed automatically.\n'
printf 'Legacy ~/media-stack/yt-dlp-server Git checkout is no longer used; do not delete it until you verify the new deployment.\n'
