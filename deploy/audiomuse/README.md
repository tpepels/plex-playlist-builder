# AudioMuse-AI — separate deployment for the Plex Playlist Manager

The AudioMuse deployment is an **independent Compose project**. It does not change Plex, yt-dlp, the existing playlist manager, or `/srv/plex-playlist-manager/playlists.yml`. **Keep the existing playlist manager as the only source of truth for playlists.** Do not enable AudioMuse's direct Plex-playlist creation/clustering writes.

## Install on media-server (Debian)

Use the existing repository checkout (typically `plex-playlist-manager`):

```bash
cd /path/to/media-stack/plex-playlist-manager
git pull --ff-only
sudo mkdir -p /srv/audiomuse-ai
sudo cp deploy/audiomuse/compose.yaml /srv/audiomuse-ai/compose.yaml
sudo cp deploy/audiomuse/.env.example /srv/audiomuse-ai/.env
cd /srv/audiomuse-ai
python3 -c 'import secrets; print(secrets.token_urlsafe(36))'
sudo nano .env
sudo chmod 600 .env
# Make sure 8098 is unused (change AUDIOMUSE_PORT in .env if necessary):
sudo ss -ltn | grep ':8098 ' || true
sudo docker compose config --quiet
sudo docker compose up -d
sudo docker compose ps
```

**Replace the `AUDIOMUSE_DB_PASSWORD` example with the freshly generated random password** before starting. Never commit the real `.env`. Use the same port for the server's firewall rules; restrict port 8098 to your trusted LAN.

Open **http://media-server:8098**, finish the browser setup wizard, configure the admin login and select:

- Provider: Plex
- Plex URL: `http://host.docker.internal:32400` (Plex uses host networking). If the server enforces HTTPS, choose a working HTTPS address with suitable certificate handling instead.
- Library filter: `Music`
- Plex authentication: supply the existing private X-Plex-Token *only inside the AudioMuse setup wizard*. Do not put it in this repository or share it in chat.

Start with a small subset and confirm Plex playback/download succeeds. Then use AudioMuse's **Analysis and Clustering → Start Analysis** to analyze the music library. **Do not start clustering that writes playlists**. Analysis can take substantial time; existing results persist if interrupted.

## Existing installation: wrong Docker tag or image pull failure

AudioMuse's Git releases use `vX.Y.Z`, but GHCR image versions use **`X.Y.Z`**, without the `v`. The previous example incorrectly used `v3.7.0`; this has been fixed. If Docker reports `ghcr.io/neptunehub/audiomuse-ai:v3.7.0: not found` on an installation that already has its own `/srv/audiomuse-ai/.env`, do **not** recopy the sample `.env`: that would overwrite its generated database password. Instead:

```bash
cd /srv/audiomuse-ai
sudo sed -i 's/^AUDIOMUSE_TAG=v3\.7\.0$/AUDIOMUSE_TAG=3.7.0/' .env
sudo docker compose config --quiet
sudo docker compose pull
sudo docker compose up -d
sudo docker compose ps
```

If the tag fails again, check the currently published tags at https://github.com/NeptuneHub/AudioMuse-AI/pkgs/container/audiomuse-ai and use an existing version (or `latest` for the released rolling version). The PostgreSQL `Interrupted` message is a consequence of the failed Docker Compose pull, not evidence of a PostgreSQL problem.

## Fast Plex ID verification (recommended first)

The first probe already confirmed the AudioMuse `/api/sync` index works. To
validate provider IDs, **do not fetch all Plex tracks**: the full library-export
endpoint can take a long time. The new implementation asks Plex for only a
bounded sample of AudioMuse track IDs, then compares their actual song titles.

First update and rebuild the existing playlist-manager **service only**:

```bash
cd ~/media-stack/plex-playlist-manager
git pull --ff-only
cd ~/media-stack
sudo docker compose up -d --build plex-playlist-manager
```

Then (the probe automatically reads the private token file created earlier):

```bash
cd ~/media-stack/plex-playlist-manager
python3 -u scripts/audiomuse_probe.py \
  --url http://127.0.0.1:8098 \
  --verify-sample 12
```

This calls **POST `/api/plex/verify-rating-keys`** in our manager (max 25 IDs
per request) and **GET `/api/sync?ids=...`** in AudioMuse, without embeddings.
Both are read-only. A successful sample has `verified: true`,
`matched: 12`, and empty `missing`/`title_mismatches`. Some metadata
differences can be benign, so investigate mismatches before interpreting them
as broken identifiers. The 12-track sample alone does **not** prove all the
library IDs match. For more coverage use `--verify-sample 25`; only
`--all` changes the AudioMuse index traversal, and neither option triggers
the slow full-library Plex export.

**Never use this validation endpoint to modify Plex playlists.** It cannot
write to Plex or the playlist YAML.

## Read-only ID mapping check (after the first analysis batch)

From the playlist-manager repository on media-server:

```bash
cd ~/media-stack/plex-playlist-manager
git pull --ff-only
python3 scripts/audiomuse_probe.py --url http://127.0.0.1:8098 --all
```

The probe displays a progress message for each manifest page and when the Plex library export starts. If the full comparison appears slow, first run `python3 scripts/audiomuse_probe.py --url http://127.0.0.1:8098 --manifest-only` for a single-page smoke test, then add `--all --manifest-only` for the whole manifest. Both modes avoid hitting the Plex playlist manager. The full comparison can take longer because the existing full-library endpoint asks Plex for every track.

The probe automatically downloads the **current** full Plex library TSV from the existing playlist manager on `http://127.0.0.1:5051/api/library-export.tsv`. It fetches the AudioMuse read-only `/api/sync?fields=index` manifest and compares exact provider IDs with Plex `rating_key` values. Nothing is modified in either system.

If AudioMuse authentication is enabled (default), configure an API token in **Administration → Setup Wizard → AudioMuse Authentication** (`API_TOKEN`). The probe reads an explicit `AUDIOMUSE_API_TOKEN` environment variable first; otherwise it loads `~/.config/plex-playlist-manager/audiomuse-api-token` automatically. This user-only file is outside the Git repository and persists after logging out.

One-time installation, **assuming the current shell still contains the API token from a previous successful request**:

```bash
install -d -m 700 "$HOME/.config/plex-playlist-manager"
if [ -z "${AUDIOMUSE_API_TOKEN:-}" ]; then
    echo "No token in this shell. Enter it separately using: read -rsp Token: AUDIOMUSE_API_TOKEN; echo"
else
    (umask 077; printf '%s' "$AUDIOMUSE_API_TOKEN" > "$HOME/.config/plex-playlist-manager/audiomuse-api-token")
    chmod 600 "$HOME/.config/plex-playlist-manager/audiomuse-api-token"
    unset AUDIOMUSE_API_TOKEN
fi
```

Run `python3 scripts/audiomuse_probe.py --url http://127.0.0.1:8098 --manifest-only` without a token variable afterward. To read a non-default location, set `AUDIOMUSE_API_TOKEN_FILE` to the path. The probe rejects token files that are readable by group/others; don't print or commit the token. `AUDIOMUSE_COOKIE` is also an optional fallback for existing setups. Explicit `AUDIOMUSE_API_TOKEN` always has priority.

Note: this token is available only to processes running as the Linux user who owns the file. The Docker playlist-manager container cannot automatically read a home-directory token file; a future in-container API integration will need a deliberate read-only secret mount.

For an explicit TSV export, replace automatic retrieval with `--plex-tsv /path/to/plex-music-library.tsv`. For a remote playlist manager use `--plex-url http://media-server:5051`. If audio analysis is still running, matching covers **only the analyzed tracks**. Do not assume the entire library is mismapped because the scanned count is low.

Important results: `provider_type` should be `plex`, `exact_plex_rating_key_matches` should normally equal `manifest_tracks_fetched` for a single Plex server, and `unmatched_sample` should be empty. A small or zero match requires checking the server configuration and mapping before proceeding.

## Start and monitor full-library analysis from the Playlist Manager

The first 336 tracks have been successfully synchronized, but 0/116 YAML
playlists had sufficient analyzed coverage. The default report now suppresses
three Plex-wide system playlists (All Music, Recently Added, Recently Played),
without deleting them from YAML or Plex.

The **Musical coherence** page can now enqueue an AudioMuse background analysis
and show the active/main-task status:

- Select **Latest 25 albums**, **Latest 100 albums**, or **Entire library**.
- **Entire library** passes \`num_recent_albums=0\` to the documented
  \`POST /api/analysis/start\` endpoint and requires an explicit confirmation.
- The status panel polls the documented \`/api/active_tasks\` and \`/api/last_task\`
  routes while the page is open. Analysis runs in AudioMuse's own worker and
  continues if you close the browser.
- This integration cannot start clustering or touch Plex playlists.
- Completed analysis is copied to Playlist Manager's local feature cache by a
  background worker. Each pass transfers at most 500 changed tracks and
  continues until the current batch is complete. The browser displays
  analysis progress, feature coverage and coherence without a Sync button.
- New albums and tracks are analyzed automatically by the Playlist Manager background worker after the Plex catalogue refresh. A durable state file prevents repeat submissions across restarts. The first scan baselines an existing library; subsequent arrivals are queued. Entire-library backfill is still a deliberate action.

Update the existing manager service only after pulling the repository:

\`\`\`bash
cd ~/media-stack/plex-playlist-manager && git pull --ff-only
cd ~/media-stack
sudo docker compose up -d --build --no-deps plex-playlist-manager
\`\`\`

Then use http://media-server:5051/musical-coherence.

**Security note:** the manager exposes other privileged operations on port
5051 already. Its new analysis-start endpoint requires a same-origin browser
POST, but the playlist manager should still be reachable only on your trusted
LAN or behind authentication. Do not publish 5051 to the public internet.

## Playlist-manager integration: audio cache and coherence report

The Plex rating-key identity sample passed (12/12). This adds an **independent
read-only feature cache** to the existing playlist manager. It does not require
a full Plex export and does not call AudioMuse's playlist-creation endpoints.

The API token already saved for your CLI probe must also be readable from
within the **playlist-manager container**. The CLI token stored under the
Linux user's home directory is not automatically visible inside Docker.

1. Edit the media stack's **actual** Compose file (usually
   ~/media-stack/docker-compose.yml). Under the **existing**
   plex-playlist-manager service's environment add:

   AUDIOMUSE_URL: http://host.docker.internal:8098
   AUDIOMUSE_API_TOKEN_FILE: /run/secrets/audiomuse_api_token

2. Under that same service's **existing** volumes append this bind mount:

   - /home/tom/.config/plex-playlist-manager/audiomuse-api-token:/run/secrets/audiomuse_api_token:ro

   Use the absolute path to the file created earlier and adjust /home/tom if
   your actual home directory differs. The file must already exist, be a file
   (not a directory), and be private (chmod 600). Never put the token value in
   docker-compose.yml or send it in chat. The Dockerfile currently runs as
   root, so it can read a root-owned Docker bind mount to the file.

3. Rebuild ONLY the playlist-manager service:

   cd ~/media-stack/plex-playlist-manager && git pull --ff-only
   cd ~/media-stack
   sudo docker compose config --quiet
   sudo docker compose up -d --build --no-deps plex-playlist-manager

4. Open http://media-server:5051/playlists?work=analysis. The background
   worker stores the analyzed tracks in
   /srv/plex-playlist-manager/audiomuse-cache.sqlite inside the manager's
   persistent data volume. New analysis and the cached feature coverage
   appear automatically; updates are incremental by AudioMuse fingerprints.

5. Inspect the page. Each playlist reports analyzed-track coverage. Only
   playlists with at least six embeddings and 60% coverage (and no unresolved
   album-only YAML entries) get an exploratory pairwise cosine score and ranked
   relative outliers. No auto-remove, auto-add, or Plex sync is done.

Each internal API request processes at most **500 changed tracks**. The UI
repeats these requests in one click until the cache is caught up, or until you
click **Stop after current batch**. Completed batches persist so the next
click resumes from the remaining tracks. A future improvement is server-side
scheduled synchronization independent of an open browser.

The source files are app/audiomuse_client.py (sync + SQLite),
app/audiomuse_coherence.py (scoring), and a dedicated read-only review
page. The manager backend reads AudioMuse through the host.docker.internal
alias already used for Plex.

**Important:** similarity alone cannot identify dissonance, free jazz, or
inappropriate tracks reliably. These are review candidates, not autonomous
curation decisions. The initial 336 tracks are too sparse to score most
playlists. Expand AudioMuse's library analysis later after the API and UI
are confirmed to work.

## Scope and resources

- Web app: port **8098** (playlist manager stays on 5051).
- Postgres 15 persists in a dedicated volume; no published database port.
- One worker, CPU-capped at 2 cores by default (`AUDIOMUSE_WORKER_CPUS`).
- Image tag pinned to **3.7.0** (Git release `v3.7.0`; GHCR image tags omit the leading `v`), instead of unpinned `latest`.
- No music-directory mount: AudioMuse streams from Plex.
- Keep the web app LAN-only or behind trusted authenticated access.
- `sudo docker compose down` preserves volumes. **Never use `down -v` unless you deliberately want to erase all AudioMuse analysis data.**

Upstream documentation: https://neptunehub.github.io/AudioMuse-AI/DEPLOYMENT/ and https://github.com/NeptuneHub/AudioMuse-AI/blob/main/app_sync.py
