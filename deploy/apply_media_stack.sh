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
            break
        print("Bridge unavailable: " + str(result.get("error", "unknown")))
    except (OSError, ValueError, HTTPError, URLError):
        if i == 14:
            print("Check: docker compose logs --tail=100 yt-dlp-server plex-playlist-manager")
            break
    time.sleep(2)
PY

printf '\nWorkspace: http://<media-server>:5051/downloads\n'
printf 'Existing Plex playlists are not changed automatically.\n'
