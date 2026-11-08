#!/usr/bin/env bash
# Migrate only Playlist Manager and yt-dlp. Preserve persistent volumes,
# other services and the original Compose file in a private backup.
set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
STACK="$(cd "$SCRIPT_DIR/../.." && pwd)"
MANAGER="$STACK/plex-playlist-manager"
ENGINE="$STACK/yt-dlp-server"

error() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
step() { printf '\n==> %s\n' "$*"; }

[ -d "$MANAGER/.git" ] || error "Missing Playlist Manager checkout: $MANAGER"
[ -d "$ENGINE/.git" ] || error "Missing yt-dlp checkout: $ENGINE"
command -v python3 >/dev/null 2>&1 || error "Python 3 is required"
command -v docker >/dev/null 2>&1 || error "Docker is required"
docker compose version >/dev/null 2>&1 || error "Docker Compose plugin required"
[ -c /dev/net/tun ] || error "Missing /dev/net/tun for WireGuard"

# The active job queue is durable. Recreating the engine while it downloads
# would unnecessarily interrupt it. Fail BEFORE pulling/building/editing.
step "Preflight: refuse migration while downloads are active or queued"
engine_running="$(docker container inspect --format '{{.State.Running}}' yt-dlp-server 2>/dev/null || true)"
if [ "$engine_running" = "true" ]; then
  docker exec -i yt-dlp-server python - <<'PY' ||
    error "Could not confirm an idle downloader; nothing was changed. Check port 4545 and the job queue."
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
fi

step "Fast-forward both repositories"
git -C "$ENGINE" pull --ff-only || error "yt-dlp git pull failed; no Compose changes made"
git -C "$MANAGER" pull --ff-only || error "Manager git pull failed; no Compose changes made"

step "Validate proposed Compose changes"
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

step "Recreate only the two download services"
if docker container inspect yt-dlp-wireguard-gateway >/dev/null 2>&1; then
    docker rm -f yt-dlp-wireguard-gateway ||
        error "Failed to remove the obsolete gateway container"
fi
docker compose up -d --no-deps --no-build --force-recreate yt-dlp-server ||
    error "yt-dlp deployment failed. Inspect container logs."
docker compose up -d --no-deps --no-build --force-recreate plex-playlist-manager ||
    error "Playlist Manager deployment failed. Inspect container logs."

step "Check the updated services"
docker compose ps yt-dlp-server plex-playlist-manager ||
    error "Could not list service status"

step "Check the download workspace through port 5051"
python3 - <<'PY'
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

for i in range(15):
    try:
        with urlopen("http://127.0.0.1:5051/api/downloads/bridge/status",
                     timeout=5) as response:
            result = json.load(response)
        if result.get("ok"):
            print("Connected: http://<media-server>:5051/downloads")
            sys.exit(0)
        print("Bridge unavailable: " + str(result.get("error", "unknown")))
    except (OSError, ValueError, HTTPError, URLError):
        if i == 14:
            print("Check: docker compose logs --tail=100 yt-dlp-server plex-playlist-manager")
            break
    time.sleep(2)
print("Bridge health check failed; inspect both container logs.", file=sys.stderr)
sys.exit(1)
PY
if [ "$?" -ne 0 ]; then
  error "Deployment did not pass the manager-to-downloader health check. Preserve the Compose backup."
fi

printf '\nWorkspace: http://<media-server>:5051/downloads\n'
printf 'Existing Plex playlists are not changed automatically.\n'
