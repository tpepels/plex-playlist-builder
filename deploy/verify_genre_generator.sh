#!/usr/bin/env bash
# Live, non-publishing verification for the integrated genre generator.
# Run from a checkout on the actual Debian media server.
# Usage:
#   bash deploy/verify_genre_generator.sh             # read-only
#   GENRE_POSITIVE_REFS="123,456" bash deploy/verify_genre_generator.sh --draft
# A draft is saved only with --draft; the script NEVER publishes to Plex/YAML.
set -u
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required on the media server." >&2
  exit 1
fi
docker exec -i plex-playlist-manager python - "$@" <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE="http://127.0.0.1:8000"
def request(path, obj=None):
    headers={"Accept":"application/json"}
    if obj is not None:
        headers.update({"Content-Type":"application/json","Origin":BASE})
    data=json.dumps(obj).encode() if obj is not None else None
    req=urllib.request.Request(BASE+path,data=data,headers=headers,
                               method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req,timeout=90) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try: reason=json.load(exc)["detail"]
        except Exception:reason=exc.reason
        raise RuntimeError(f"{path}: HTTP {exc.code}: {reason}") from exc

mode=sys.argv[1] if len(sys.argv)>1 else "--status"
if mode not in ("--status","--draft"):
    raise SystemExit("Usage: verify_genre_generator.sh [--status|--draft]")
try:
    status=request("/api/generation/readiness")
    print("Plex tracks:",status["plex_tracks"])
    print("AudioMuse manifest tracks:",status.get("audiomuse_available"))
    print("Matched IDs:",status["matched_track_ids"])
    print("MusiCNN embeddings:",status["musicnn_available"])
    print("CLAP embeddings:",status["clap_available"])
    print("Missing valid embeddings:",status["missing_embeddings"])
    print("Pending records:",status.get("pending"))
    print("Analysis cache ready:",status["ready"])
    print("Last synchronized:",status.get("last_sync"))
    profile=request("/api/generation/genre-profiles?genre=jazz&subgenre=hard%20bop")
    chosen=profile["profiles"][0] if profile["profiles"] else {}
    print("Tagged Hard bop examples:",chosen.get("verified_metadata_count",0))
    print("Analyzed tagged references:",chosen.get("analyzed_references",0))
    if mode=="--status":
        if not status["ready"]:
            raise RuntimeError("AudioMuse/Plex cache is incomplete. Sync from Create before accepting live generation.")
        print("Read-only live readiness check passed. No Plex/YAML changes made.")
    else:
        if not status["ready"]:
            raise RuntimeError("AudioMuse cache is not ready; refusing a misleading draft.")
        raw=os.environ.get("GENRE_POSITIVE_REFS","")
        references=[x.strip() for x in raw.split(",") if x.strip()]
        if not chosen.get("available") and len(references)<2:
            raise RuntimeError("Supply two verified Plex track IDs through GENRE_POSITIVE_REFS.")
        brief={
            "prompt":"Hard bop jazz with balanced artist variety and smooth transitions.",
            "name":"Hard bop acceptance draft "+datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ"),
            "count":30,"genre":"jazz","subgenre":"hard bop",
            "reference_track_ids":references,"negative_reference_track_ids":[],
            "identity":{"variation":"balanced","flow":"smooth"},
        }
        result=request("/api/generation/drafts",brief)
        data=result["data"]
        keys=data["keys"]
        if len(keys)!=30 or len(set(keys))!=30 or len(data.get("genre_evidence",{}))!=30:
            raise RuntimeError("Draft failed exact 30-track/evidence acceptance.")
        print("Saved unpublished draft:",result["id"],"revision",result["revision"])
        print("Tracks:",len(keys),"evidence:",len(data["genre_evidence"]))
        print("Open /playlist-generator and Restore last draft to inspect before publication.")
        print("This script never writes Plex playlists or the playlist YAML.")
except Exception as exc:
    print("LIVE ACCEPTANCE INCOMPLETE:",exc,file=sys.stderr)
    raise SystemExit(1)
PY
