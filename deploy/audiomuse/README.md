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

## Read-only ID mapping check (after the first analysis batch)

From the playlist-manager repository on media-server:

```bash
cd ~/media-stack/plex-playlist-manager
git pull --ff-only
python3 scripts/audiomuse_probe.py --url http://127.0.0.1:8098 --all
```

The probe automatically downloads the **current** full Plex library TSV from the existing playlist manager on `http://127.0.0.1:5051/api/library-export.tsv`. It fetches the AudioMuse read-only `/api/sync?fields=index` manifest and compares exact provider IDs with Plex `rating_key` values. Nothing is modified in either system.

If AudioMuse authentication is enabled (default), configure an API token in **Administration → Setup Wizard → AudioMuse Authentication** (`API_TOKEN`). Pass it to the probe via `AUDIOMUSE_API_TOKEN` on the server. Do not paste it into chat, commit it or include it in screenshots. You can also use an existing local browser cookie through `AUDIOMUSE_COOKIE` if needed. The API token takes precedence.

For an explicit TSV export, replace automatic retrieval with `--plex-tsv /path/to/plex-music-library.tsv`. For a remote playlist manager use `--plex-url http://media-server:5051`. If audio analysis is still running, matching covers **only the analyzed tracks**. Do not assume the entire library is mismapped because the scanned count is low.

Important results: `provider_type` should be `plex`, `exact_plex_rating_key_matches` should normally equal `manifest_tracks_fetched` for a single Plex server, and `unmatched_sample` should be empty. A small or zero match requires checking the server configuration and mapping before proceeding.

## Scope and resources

- Web app: port **8098** (playlist manager stays on 5051).
- Postgres 15 persists in a dedicated volume; no published database port.
- One worker, CPU-capped at 2 cores by default (`AUDIOMUSE_WORKER_CPUS`).
- Image tag pinned to **3.7.0** (Git release `v3.7.0`; GHCR image tags omit the leading `v`), instead of unpinned `latest`.
- No music-directory mount: AudioMuse streams from Plex.
- Keep the web app LAN-only or behind trusted authenticated access.
- `sudo docker compose down` preserves volumes. **Never use `down -v` unless you deliberately want to erase all AudioMuse analysis data.**

Upstream documentation: https://neptunehub.github.io/AudioMuse-AI/DEPLOYMENT/ and https://github.com/NeptuneHub/AudioMuse-AI/blob/main/app_sync.py
