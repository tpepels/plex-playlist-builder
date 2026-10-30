# Plex Playlist Manager

A tiny local web service for defining Plex music playlists in YAML, previewing matches against your Plex Music library, and syncing them idempotently.

## What it does

- Reads playlist definitions from `/data/playlists.yml`.
- Supports whole albums and individual tracks.
- Preserves the YAML item order and Plex album track order.
- De-duplicates tracks automatically.
- Previews every match before you sync.
- `strict: true` prevents any change if an item cannot be matched.
- Sync replaces the existing Plex playlist of the same name, making the YAML file the source of truth.
- Reads the Plex token automatically from a **read-only** mount of Plex's `Preferences.xml`; `PLEX_TOKEN` can be used instead.
- Includes a small browser editor at port 5051.
- Exports a compact album-only TSV (one unique artist/album pair per row) for checking new music recommendations against the existing collection.
- Exports the complete Plex Music library at track level as TSV for playlist design in ChatGPT (artist, album, title, disc/track number, year, duration, and Plex rating key).
- Exports recently added music directly as TSV: last X hours (new tracks only) or last N added albums (complete albums), with their Plex rating keys and addition timestamps.
- Exports all current Plex audio playlists as importable YAML, preserving playlist order and exact Plex rating keys so the file can be uploaded in chat to avoid reusing tracks.

## Install into your existing media-stack

Assuming your compose file is in the directory that contains your other project directories:

```bash
cd /path/to/media-stack
git clone https://github.com/tpepels/plex-playlist-builder.git plex-playlist-manager
sudo mkdir -p /srv/plex-playlist-manager
sudo cp plex-playlist-manager/playlists.example.yml /srv/plex-playlist-manager/playlists.yml
```

Add the service from `docker-compose.snippet.yml` to your existing `services:` block, then:

```bash
docker compose up -d --build plex-playlist-manager
docker compose logs -f plex-playlist-manager
```

Open:

```text
http://media-server:5051
```

(or use the server's IP address).

## Compose service

```yaml
  plex-playlist-manager:
    build:
      context: ./plex-playlist-manager
    container_name: plex-playlist-manager
    ports:
      - "5051:8000"
    environment:
      TZ: ${TZ}
      PLEX_URL: https://host.docker.internal:32400\n      PLEX_VERIFY_SSL: "false"
      PLEX_LIBRARY: Music
      PLAYLISTS_FILE: /data/playlists.yml
      YTDLP_SERVER_URL: http://yt-dlp-server:4545
      YTDLP_PUBLIC_URL: http://media-server:4545
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - /srv/plex-playlist-manager:/data
      - /srv/plex/config:/plex-config:ro
    restart: unless-stopped
```

### Why `host.docker.internal`?

Your Plex container uses `network_mode: host`, so Plex listens on the Docker host at port 32400. This service runs on a normal bridge network and reaches the host through Docker's `host-gateway` alias. Nothing in the existing Plex service needs to change.\n\nIf Plex enforces HTTPS, use `PLEX_URL: https://host.docker.internal:32400`. Because Plex's local certificate normally does not match the `host.docker.internal` hostname, set `PLEX_VERIFY_SSL: "false"` for this trusted local connection. Leave verification enabled when using a hostname with a valid matching certificate.

## Export current Plex playlists

Use **Export Plex playlists** in the web UI, or download directly from:

```text
/api/playlists-export.yml
```

The export contains every audio playlist currently in Plex. Each track includes its exact Plex `rating_key` plus readable artist, album, and track metadata. Playlist order is preserved. The resulting `plex-playlists.yml` is also valid input for the playlist builder, so it can be archived, edited, or uploaded to ChatGPT when designing new playlists.

## Export a compact list of owned albums

Click **Album list TSV** in the import/export toolbar to download `plex-albums.tsv`,
or open `/api/library-albums.tsv` directly.

The file has only two columns, `artist` and `album`. Each distinct
artist/album title pair appears once (case-insensitive deduplication), sorted
alphabetically. Different artists with the same album title and different
editions with different titles remain separate. Tracks without an album title
are omitted. It reflects the current Plex Music library and is read-only.

Use this export when asking for **albums not already in Plex**. Use **Full
library TSV** when individual songs, track order, or rating keys matter.

## Export recent music

In the compact toolbar, select **New music → Last hours** (default 48), or
**New music → Latest albums** (default 20), choose a number, and click **Download TSV**.

- **Last hours:** includes only tracks whose Plex `addedAt` is within the selected
  number of hours (1–8760), including newly added tracks on older albums.
- **Latest albums:** selects the N most recently added Plex albums (1–200) and exports
  **all** their tracks in album/disc order.
- Both exports contain artist, album, track name, disc/track number, year, duration,
  `rating_key`, and `added_at` (UTC ISO 8601). For the album mode, `added_at`
  means the **album** addition timestamp; in hours mode, it is the **track**
  addition timestamp. Empty selections still return a TSV header.
- The export is read-only; it does not change playlists or any music files.

The endpoint is `/api/library-recent.tsv?mode=hours&limit=48`, or
`/api/library-recent.tsv?mode=albums&limit=20`.

## Discover: In Sheep's Clothing Hi-Fi

The **Discover** tab is a read-only source of album ideas from
[In Sheep's Clothing's public album collection](https://insheepsclothinghifi.com/collection/).
The dashboard surveys the newest visible collection entries in eight
source-defined sections: Japan, Jazz, Funk, Fusion, Ambient,
Experimental, Pop and Electronic. Linked albums under the site's **Suggested Records** recommendations
are included with explicit attribution to the recommending record; they do
**not** automatically inherit its genre. This is a **bounded selection, not
the site's entire historical catalog**. A limited number of linked album
pages are enriched with their published styles, tags and release years.
The app stores only concise album metadata and source URLs, not editorial
articles or artwork.

Every entry is compared to all Plex Music tracks on **artist + album**,
normalizing punctuation and known Deluxe/Remastered edition suffixes. Strict
artist identification prevents counting a different musician's identically
titled album as owned. It labels a record as **In Plex**, **Other edition in
Plex**, or **Missing**; owned records show the corresponding Plex album title
and track count. No playlist definitions are changed.

Filter by status, artist/title search and five optional **local listening
categories**: Japanese jazz-funk, Deep listening, ECM-like jazz, Ambient and
Experimental pop. These are **our suggested moods inferred only from source
genre/tag metadata**, not claims about the source's own playlist categories.
Albums without evidence for these moods remain visible under All styles.

Each record links to its originating ISC album page. Missing and
edition-different records also have **Review and queue album**, using the
`/open-youtube-album` resolver. GET requests only show a preview and a
signed confirmation form. **Only an explicit form POST** submits the verified
album to the downloader's idempotent `POST /api/albums` endpoint and then
opens the downloader's jobs page. Repeated submissions of the same YouTube
album ID return the original job ID. An unmatched album opens a YouTube Music
search for exactly `artist album`, without a download. Discovery itself
does not alter playlist definitions.

The catalog is cached for 24 hours in /data/isc-discovery.json so normal
dashboard visits do not repeatedly scrape the site; **Refresh source**
bypasses the cache. A failed refresh uses the previous catalog with a
staleness warning, if one exists. Source failures are reported rather than
inventing releases. A temporary network problem without a cache returns 503.

API: GET /api/discover/isc?refresh=false.

## Duplicate album and directory review

The **Duplicates** tab compares **complete album releases**, not individual
duplicate songs. It groups Plex music by normalized artist and album, using
each physical album directory as an independent copy when file paths are
available from Plex Media/Part metadata. It recognizes common multidisc
layouts, where tracks live in Disc 1 / Disc 2 subdirectories.

- **Complete duplicate album**: full tracklists match, including disc and
  track positions, titles and durations within 2 seconds. Both physical
  album folders are shown; choose which copy to keep after checking quality.
- **Alternate edition**: matching tracks appear on albums whose titles only
  differ in recognized release labels such as Deluxe or Remaster. These
  remain **review-only**, never automatic folder-removal candidates.
- **Partial overlap**: tracklists have extras, omissions or different takes.
  These also remain review-only.
- **Folder review candidates**: only complete matches where Plex exposes
  an album directory, every track has a path, and the folder appears
  dedicated to a single album in Plex's indexed metadata. Shared artist or
  multi-album folders are blocked.

Each complete-album group has a **Keep this folder** selector. Once you
choose the copy to retain, other eligible dedicated album folders are
highlighted **Review for removal**. This choice is only a temporary UI aid:
it does not remove, move or save any files. The UI also shows file formats
(e.g. FLAC vs MP3) and offers a **Copy path** button. The TSV export has
**one row per album copy**, including folder, file formats, track count,
Plex album/track keys and comparison category. Filter by artist, album
or directory, or download the complete report. Refresh bypasses the
five-minute Plex track cache.

**Safety limitations:** This uses metadata, not audio fingerprints or
checksums. Plex paths may be inside a different filesystem namespace from
the host. Plex cannot tell this tool about unindexed files, artwork, cue
sheets or other contents of a directory, or guarantee identical audio
quality. A folder marked as a review candidate is **not confirmed safe to
delete**. Verify its contents and keep at least one complete album before
manually removing anything. The app never deletes, renames, moves, or edits
media files, nor does the scanner modify Plex playlists.

API: GET /api/duplicates?refresh=false and GET /api/duplicates.tsv.

## Health, New Music and Discover

The top navigation has five views: **Playlists**, **Health**, **Duplicates**, **New music**,
and **Find album**. Health and New music compare the saved playlist definitions
with the current Plex music library; Plex tracks are cached for five minutes,
and **Refresh** forces a live rescan.

**Health** reports unique Plex tracks not included in any saved playlist,
unresolved playlist references (deleted/moved tracks), tracks appearing in
more than one playlist, and per-playlist artist/album diversity, overlap
across playlists and recently added music.

Playlists are **Curated** unless you explicitly mark them **Rotating** in the
Health view. Status lives in the separate `/data/playlist-policies.yml` file,
so changing a policy never changes the playlist definitions or triggers a
Plex sync. The initial advisory targets for rotating playlists (12+ tracks):
no more than 20% from one artist, 25% from one album, and at least 15% of
tracks added to Plex during the selected recent window. These are **warnings,
not instructions to automatically remove or add tracks**. There is no
automatic playlist rotation.

**New music** shows albums with at least one track added to Plex within the
selected 7/30/90/365-day window. You can filter for albums whose playlist
coverage is under 50%. Open an album to select **specific tracks**, choose an
existing destination playlist, and click **Add selected**. Only the selected
tracks are appended; duplicates are skipped and the saved definition is
version-checked against concurrent changes. The chosen playlist is then
synced to Plex using the existing per-playlist sync function; failure leaves
the definition pending for retry. Playlist curation is always opt-in.

**Find album** accepts artist and album and queries YouTube Music's public
album catalog (no user account or API key needed). It returns a **direct album
URL** only when the artist and title match and the result has a valid YouTube
Music album ID.

The resolver uses strictly defined canonicalization: punctuation, `&` / `and`,
`Vol.` / `Volume`, and `Pt.` / `Part` are equivalent. Only a fixed list of
trailing release qualifiers (e.g. Deluxe, Remastered, Complete, Expanded) can
be removed, and a requested **Complete** release will not resolve to a shorter
edition. `(Live)`, `(Demo)`, `(Acoustic)`, `(Mono)`, `(Stereo)`, and volume
numbers remain meaningful. Two exceptions are explicitly documented in the
resolver: Stan Getz's *Getz at the Gate (Live)* and Miles Davis's *Blackhawk
Complete (Live)* catalog label. New exceptions require intentional code changes.

All bounded catalog searches are evaluated before choosing a result. Exact
titles rank above recognized editions and explicit aliases. Two different
album IDs tied at the best rank yield `status: ambiguous`: neither is sent
to the downloader. The resolved response includes the applied
`normalization_rules`, search queries and candidate count, which are passed
to the downloader for persistent provenance.

API routes: `GET /api/youtube-album?artist=...&album=...` for read-only
resolution, `GET /open-youtube-album?artist=...&album=...` for read-only
confirmation, and `POST /queue-youtube-album` for the signed user action.
A missing album redirects to a YouTube Music search for exactly `artist album`.
An ambiguous match displays candidates but never submits a job.

The POST first checks the current Plex Music library for the exact normalized
artist/title pair. An owned album is rejected with HTTP 409. Otherwise, the
manager prefers an `OLAK5uy_` audio playlist over a `browse/MPRE...` link,
submits JSON to the yt-dlp server's `POST /api/albums`, and receives a stable
`job_id`, `duplicate` flag, and current job status. On network timeout the
manager does not claim success; retrying the same album is safe because the
downloader persists the album-ID key alongside job state across restarts.
Submission audit records include requested/matched titles, matched URL, search
queries and applied normalization rules. A queue response confirms admission,
not successful download or completed metadata validation.

Configure `YTDLP_SERVER_URL` (default `http://yt-dlp-server:4545`) to reach
the downloader over the shared Docker network. `YTDLP_PUBLIC_URL` (default
`http://media-server:4545`) is the browser-facing downloader address. For
multiple playlist manager workers, configure the same `ALBUM_ACTION_SECRET`
on all workers so signed form tokens remain valid across workers. The default
uses a per-process random signing key. The yt-dlp service must be updated to
include `/api/albums` before this version of the playlist manager is deployed.

**Note:** The shared links are now safe to open or preview. Clicking the
confirmation button initiates the download. Album titles with equal best
catalog rankings are deliberately not downloaded automatically.

## Playlist versions and sync status

The dashboard now separates the **saved YAML version** from its **Plex sync state**.

- **Loaded in the builder** shows a content-based 12-character ID. The ID is based on playlist definitions (not YAML whitespace or the order of playlist names).
- After importing a file, the dashboard shows its **filename** and **import time**. This history is saved in `playlist-state.yml` next to your saved definitions.
- **Import YAML and sync** validates the selected YAML file, immediately imports valid definitions (new and changed playlists) and automatically starts syncing pending, enabled playlists to Plex. Existing playlists absent from the selected file are retained. Invalid YAML does not change the saved definitions. No separate 'Load this version' or 'Apply changes to Plex' click is needed; Plex errors stay visible with a retry option.
- **Needs Plex update** indicates a changed/new definition not yet synced by this builder.
- **Synced by builder** means the currently loaded definition matches the revision most recently synced by this app.
- **Sync not verified** means an old/external playlist has no recorded successful sync in this app. The builder does **not** query Plex playlist contents to verify exact parity.
- The YAML editor warns about unsaved changes and blocks import/sync until the edits are saved or discarded.
- The dashboard shows a compact version summary, import/export controls, and collapsed playlists. **Plex updates** only appears when there are pending changes or a sync in progress / failed.
- **Sync changed playlists to Plex** processes all new or updated, enabled playlists with one click. It shows progress and errors, lets you stop after the current playlist, and skips unchanged/synced, unverified, disabled and Plex utility playlists. Failed playlists remain pending and can be retried using the same button.
- Bulk sync performs short **sequential requests from the browser** rather than running in the background. Keep the browser page open until it finishes. Reloading the page stops subsequent requests; completed playlists stay marked as synced.
- A bulk run is tied to the loaded content ID. If another import or edit changes the saved playlist pack during the run, the server rejects further updates instead of syncing a different version.

A different content ID proves a file's definitions differ; it **cannot** prove which version was created later. A downloaded file is not automatically loaded merely by being present on disk. Import it explicitly to make the builder use it.

Existing `playlist-state.yml` files with a legacy `pending` list remain supported. Importing a valid YAML file automatically synchronizes new and changed, enabled playlists to Plex (and may replace existing Plex playlists with the same titles). Manually editing the saved YAML in the advanced editor still requires a separate sync. If an automatic sync fails, its playlist remains pending so the sync button can retry it.

## Playlist covers: ZIP upload in the web UI

Use **Upload cover ZIP** at the top of the Playlist Manager web interface.
Select a ZIP containing square JPG, JPEG or PNG images in its top level.
**Each filename without the extension must exactly match a Plex audio playlist title**
(Unicode NFC-equivalent names are accepted, but no fuzzy matching or case folding).
Example: `3 AM.jpg` for the playlist `3 AM`.

The manager saves all valid, unambiguous images to
`/data/playlist-covers/` on the existing persistent `/data` volume.
With the sample Docker Compose configuration they remain at
`/srv/plex-playlist-manager/playlist-covers/` on the host.
No additional Docker mount, Python package, or cover recognition is required.

- A new image for an existing playlist name **replaces its previous version**,
  even if the format changes from JPG to PNG or vice versa.
- After saving, the manager immediately uploads matching covers to Plex.
  Unmatched images are saved for later; the results panel shows new, updated,
  applied, unmatched, pending, failed, and skipped counts and file details.
- **Playlist synchronization automatically reapplies the stored cover**
  whenever Plex deletes and recreates a managed audio playlist.
- Only top-level JPG/PNG files are accepted. Unsafe paths, duplicate images
  for the same playlist within one ZIP, and invalid images are skipped.
  Limits: 70 MiB compressed ZIP, 150 MiB expanded contents, 400 archive
  entries and 12 MiB per image.
- If Plex is offline, successfully imported images remain on disk; re-upload
  the same ZIP after reconnecting, or sync affected playlists to restore them.

API: `POST /api/playlist-covers/import-zip` with the raw ZIP body and
`Content-Type: application/zip`. The JSON response reports counts and
per-file outcomes. It does not edit playlist YAML or music files.

The older command-line `scripts/upload_playlist_covers.py` remains optional
for anyone who prefers managing covers directly on the host.
## YAML format

```yaml
playlists:
  Quiet Folk:
    strict: true
    items:
      - artist: Nick Drake
        album: Pink Moon
      - artist: Bob Dylan
        track: Moonshiner
        album: The Bootleg Series Volumes 1-3 (Rare & Unreleased) 1961-1991
```

For an album, every track on the matched Plex album is added. For a track, `album` is optional but useful when Plex contains several recordings with the same title.

## Safety / behaviour

The service never edits tags or files. It only creates/deletes Plex playlist metadata.

A sync deletes an existing audio playlist with the exact same title and recreates it from YAML. Therefore do not manually curate a playlist that is also managed here: YAML should be its source of truth.

`strict: true` is recommended while setting up a playlist. Preview first; once all entries match, sync it. `strict: false` creates the playlist from the entries that did match and reports the missing entries.

## Token handling

Preferred setup: keep the supplied read-only mount:

```yaml
- /srv/plex/config:/plex-config:ro
```

The service reads `PlexOnlineToken` from:

```text
/plex-config/Library/Application Support/Plex Media Server/Preferences.xml
```

If you don't want to mount the Plex config, remove that volume and set `PLEX_TOKEN` in the environment instead.

## Health check

```bash
curl http://localhost:5051/api/health
```

Expected:

```json
{"ok":true,"server":"...","library":"Music","version":"..."}
```
