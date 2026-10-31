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

## Read-only mapping check

From a working copy of the playlist-manager repository, download `plex-music-library.tsv` from the existing app, then:

```bash
python3 scripts/audiomuse_probe.py \
  --url http://media-server:8098 \
  --plex-tsv /path/to/plex-music-library.tsv \
  --all
```

The probe only reads the AudioMuse `/api/sync?fields=index` manifest and the local TSV; it never modifies playlists. If AudioMuse requires a logged-in session, pass the cookie in the `AUDIOMUSE_COOKIE` environment variable; don't print or check it into source control.

**Inspect `exact_plex_rating_key_matches`** before assuming AudioMuse's returned IDs equal Plex's rating keys. A low number means we need a verified identifier mapping, not a heuristic replacement.

## Scope and resources

- Web app: port **8098** (playlist manager stays on 5051).
- Postgres 15 persists in a dedicated volume; no published database port.
- One worker, CPU-capped at 2 cores by default (`AUDIOMUSE_WORKER_CPUS`).
- Image tag pinned to **v3.7.0**, instead of unpinned `latest`.
- No music-directory mount: AudioMuse streams from Plex.
- Keep the web app LAN-only or behind trusted authenticated access.
- `sudo docker compose down` preserves volumes. **Never use `down -v` unless you deliberately want to erase all AudioMuse analysis data.**

Upstream documentation: https://neptunehub.github.io/AudioMuse-AI/DEPLOYMENT/ and https://github.com/NeptuneHub/AudioMuse-AI/blob/main/app_sync.py
