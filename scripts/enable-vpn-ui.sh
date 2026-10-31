#!/usr/bin/env bash
# Idempotent one-command migration of the EXISTING parent ~/media-stack Compose.
# Never overwrite an existing override or interrupt active/queued downloads.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repository="$(cd -- "$script_dir/.." && pwd)"
parent="$(cd -- "$repository/.." && pwd)"
base="$parent/docker-compose.yml"
override="$parent/docker-compose.override.yml"
source_override="$repository/compose.parent-vpn.yaml"

if [[ ! -f "$base" ]]; then
  echo "Expected shared Compose file at $base; nothing changed." >&2
  exit 2
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker Compose must be installed on the media server." >&2
  exit 2
fi

installed=0
if [[ -e "$override" || -L "$override" ]]; then
  if [[ "$(readlink -f -- "$override")" != "$source_override" ]]; then
    echo "Existing $override will not be overwritten." >&2
    echo "The VPN service must be merged with that existing override first." >&2
    exit 2
  fi
else
  ln -s "yt-dlp-server/compose.parent-vpn.yaml" "$override"
  installed=1
fi
cleanup() {
  if [[ "$installed" == "1" && "${ready:-0}" != "1" ]]; then
    rm -f -- "$override"
  fi
}
trap cleanup EXIT

cd "$parent"
if ! docker compose -f "$base" -f "$override" config --quiet; then
  echo "Compose cannot apply the VPN override; old deployment untouched." >&2
  exit 2
fi

# A GET to the existing downloader is the only state inspection performed.
# Never stop a job merely to switch Compose networking.
existing="$(docker compose -f "$base" ps -q yt-dlp-server || true)"
if [[ -n "$existing" && "$(docker inspect -f '{{.State.Running}}' "$existing" 2>/dev/null || true)" == "true" ]]; then
  if ! python3 - <<'PY'
import json
import sys
from urllib.request import urlopen

try:
    with urlopen("http://127.0.0.1:4545/api/jobs", timeout=5) as response:
        jobs = json.load(response)
    if not isinstance(jobs, list):
        raise ValueError("Unexpected downloader status")
    busy = [j for j in jobs
            if j.get("status") in {"running", "queued", "finalizing"}
            or j.get("active_resolution_tracks")]
    if busy:
        print("The downloader has active or queued jobs. Try again when idle.")
        sys.exit(1)
except (OSError, ValueError, KeyError) as exc:
    print("Cannot verify downloader idle; no services have been stopped:", exc)
    sys.exit(1)
PY
  then
    exit 2
  fi
fi

ready=1
echo "Migrating the existing downloader. Music, state and staging stay mounted."
docker compose -f "$base" -f "$override" stop yt-dlp-server
docker compose -f "$base" -f "$override" up -d --build --force-recreate wireguard-gateway yt-dlp-server

echo
echo "VPN interface installed. Open http://media-server:4545, expand Surfshark VPN,"
echo "upload WireGuard .conf files, and click Test all countries once."
echo "Future normal 'docker compose' commands from $parent load the override automatically."
