# yt-dlp music server

Small LAN web interface around [yt-dlp](https://github.com/yt-dlp/yt-dlp) and [spotDL](https://github.com/spotDL/spotify-downloader). Paste a media URL, queue an audio-only download, stage it outside the Plex library, validate collection metadata, and only then publish it into a Plex-friendly music tree. Spotify URLs use Spotify metadata while spotDL matches audio from YouTube Music, YouTube, Bandcamp, then SoundCloud.

## What it does

- Web UI on port **4545**
- Spotify tracks, albums, playlists and artists via spotDL 4.5.2
- Automatic Spotify audio fallback: YouTube Music -> YouTube -> Bandcamp -> SoundCloud
- Manual missing-track resolver using a direct YouTube, Bandcamp or SoundCloud URL while preserving Spotify metadata
- Automatically probes pasted links before download
- Shows whether yt-dlp sees a single item or playlist/album, including item count when available
- Detects YouTube auto-generated album playlists and keeps compilation albums together with a shared Album Artist
- Shows per-track playlist progress and keeps expanded logs open while the UI refreshes
- Playlist/album downloading enabled by default
- One download worker
- Audio only
- Keeps the best source audio format instead of transcoding everything to MP3
- Embeds source metadata and cover art when available
- Downloads every job into persistent staging under `/data/tmp/staging/` first
- Publishes into the Plex library only after the complete job and metadata validation succeed
- Uses collection-level metadata for albums/playlists instead of per-video uploader metadata
- Never promotes arbitrary YouTube uploader/channel names to album artists
- Stops in **Needs metadata** when artist or album cannot be derived, with editable fields in the UI
- Retags the completed staged files with the validated album/album-artist metadata before publishing
- Keeps all final imports isolated under `YT-DLP Imports/`
- Uses persistent yt-dlp and spotDL archives to avoid accidental duplicates and support retry/continue
- Includes ffmpeg, Deno, yt-dlp EJS support, curl-cffi, spotDL, and the BgUtils PO-token provider in the image
- Optional `cookies.txt` support for sources that require login

## Library layout

Default output:

    /data/music/
      YT-DLP Imports/
        Artist/
          Album/
            01 - Track [source-id].opus

The extension follows the best source audio. The source ID is kept in the filename to prevent collisions.

Downloads are **not** written directly to that tree. They first live under:

    /data/tmp/staging/<job-id>/

For YouTube/YouTube Music the server writes yt-dlp info JSON sidecars into staging, then evaluates the whole collection together. For playlists, one collection-level album name is selected (normally the playlist/album title), and arbitrary uploaders such as the person who happened to upload one video are never treated as album artists.

If both artist/album-artist and album are trustworthy, the server retags staged audio and publishes the complete collection. **Album imports also require a successful download, a known expected track count, no unresolved failures, and exactly that many staged audio files.** Otherwise the files remain in staging, outside Plex, even if the source or user has marked a missing track ignored. If metadata is unresolved, a complete album becomes **Needs metadata** until the user confirms it. A matching count cannot establish exact track identity; independent track-level verification is still advisable.

## Add it to the existing media-server compose

Assuming this repository is cloned as `./yt-dlp-server` next to the compose file:

    yt-dlp-server:
      build:
        context: ./yt-dlp-server
      container_name: yt-dlp-server
      user: "${UID:-1000}:${GID:-1000}"
      ports:
        - "4545:4545"
      environment:
        TZ: ${TZ}
        PORT: 4545
        MUSIC_ROOT: /data/music
        IMPORT_SUBDIR: YT-DLP Imports
        STATE_DIR: /data/state
        TEMP_DIR: /data/tmp
      volumes:
        - /srv/completed/music:/data/music
        - /srv/yt-dlp-server/state:/data/state
        - /srv/yt-dlp-server/tmp:/data/tmp
      restart: unless-stopped

Prepare writable state directories once:

    sudo mkdir -p /srv/yt-dlp-server/state /srv/yt-dlp-server/tmp
    sudo chown -R "$(id -u):$(id -g)" /srv/yt-dlp-server

Then:

    docker compose build yt-dlp-server
    docker compose up -d yt-dlp-server

Open `http://YOUR-SERVER-IP:4545`.


## Optional Surfshark VPN for downloader only

This feature is **off by default**. Use Gluetun with Surfshark WireGuard to
route the yt-dlp server's outbound traffic through Surfshark, without
changing the VPN routing of Plex, Transmission, Caddy or other services.
The downloader's port 4545 remains published on the Docker host through
Gluetun. Inside the same Compose network, Gluetun aliases the name
`yt-dlp-server` for clients that already call that host.

1. Surfshark website: VPN > Manual setup > WireGuard. Generate a fresh
   WireGuard key pair, then download a server config and copy its IPv4
   `Address` CIDR field (not the VPN server's public IP).
2. On the server, inside the yt-dlp-server repository:

   ```sh
   cp .env.surfshark.example .env.surfshark
   chmod 600 .env.surfshark
   # Edit the private key, WireGuard address, region and control key.
   python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
   ```

   Put the generated random string into `SURFSHARK_CONTROL_API_KEY`. Never
   commit the completed file, send it in chat, or expose the control server's
   port 8000 to the LAN. Docker environment inspection by host administrators
   can reveal these settings; keep Docker access restricted.

3. Use the **same Compose project** and the Surfshark override. For this
   repository's standalone Compose file:

   ```sh
   docker compose --env-file .env.surfshark \
     -f compose.yaml -f compose.surfshark.yaml config -q
   # Stop only the old downloader first: it currently owns host port 4545.
   docker compose -f compose.yaml stop yt-dlp-server
   docker compose --env-file .env.surfshark \
     -f compose.yaml -f compose.surfshark.yaml \
     up -d --build --force-recreate surfshark-vpn yt-dlp-server
   ```

   Do this only when no downloader job is running. The explicit service
   names prevent rebuilding or restarting unrelated media services. If using a parent Compose
   project containing other media services, apply the
   `yt-dlp-server/compose.surfshark.yaml` override **to that same parent
   Compose file** and use its existing project name/network. Do not start
   a second downloader against the same staging/state volumes. Paths in
   Compose overrides resolve relative to the first Compose file.

4. Check VPN status and its reported public IP from the downloader's shared
   namespace:

   ```sh
   docker exec yt-dlp-server python /app/surfshark_vpn.py status
   ```

   Compare this against your usual non-VPN public IP before retrying failed
   albums. Confirm the downloader web UI remains reachable on port 4545 and
   any playlist manager that uses `http://yt-dlp-server:4545` can still
   reach it.

### Exit IP changes

A manual **bounded reconnect** can be requested only while the downloader
has no queued, running or finalizing jobs:

```sh
docker exec yt-dlp-server python /app/surfshark_vpn.py rotate
```

The command prints the old/new public IP and reports `changed: false` if the
reconnect reused the same Surfshark exit. A WireGuard reconnect can choose
the same server, so it is **not** a guaranteed IP switch.

For an explicit geographic change, edit
`SURFSHARK_SERVER_COUNTRIES` in `.env.surfshark` and re-create **both**
`surfshark-vpn` and `yt-dlp-server` with the Compose command above, only
while the downloader is idle. Recreating just Gluetun can leave the
downloader on an obsolete network namespace. Note that other countries
may have stricter geographic restrictions.

Optionally set `SURFSHARK_ROTATE_ON_403=1` to attempt at most one VPN
reconnect after repeated YouTube HTTP 403 failures, before the last normal
yt-dlp retry. This happens only **between** completed yt-dlp subprocesses
and is disabled by default. A fresh yt-dlp process re-extracts media URLs.
If the VPN comes back with the same exit IP, the logs say so rather than
claiming a switch. This never bypasses PO-token checks and is not a
guaranteed 403 solution. Do not set a short repetitive rotation interval.

The default non-VPN deployment remains unchanged if the override is not
used. Existing persistent staged files and job history are shared by the
two configurations; they must never be started simultaneously.

## Chrome extension

The running web app also exposes the same ZIP directly from the page through the **Chrome extension** button. The download is served by the yt-dlp server itself at `/chrome-extension.zip`, so users do not need GitHub access once the server is running.

A ready-to-share package is available directly from the repository:

[Download Chrome extension v1.0.0](https://github.com/tpepels/yt-dlp-server/raw/refs/heads/main/dist/yt-dlp-server-chrome-extension-v1.0.0.zip)

Recipients only need to unzip it, open `chrome://extensions`, enable **Developer mode**, choose **Load unpacked**, and select the unzipped folder.

The repository includes a Manifest V3 Chrome/Chromium extension in `chrome-extension/`.

It adds **Send to yt-dlp server** to the right-click menu for:

- links;
- selected text containing one or more URLs;
- selected hyperlink text, even when the visible text is not itself a URL.

Selected text may contain up to 20 unique URLs. Each URL is first passed through the server's normal preflight probe, then queued with playlist/album mode enabled. Non-album links are still accepted, but the extension shows the same kind of small advisory warning rather than blocking them.

Install it locally:

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Choose **Load unpacked**.
4. Select the `chrome-extension` directory.
5. Click the extension icon (or open **Details -> Extension options**).
6. Enter the server URL, for example `http://media-server:4545`, and choose **Save and test**.

The extension does not request blanket host access at install time. It asks Chrome for permission only to the configured server origin. Page access for reading a right-click selection uses `activeTab`, which is temporary and tied to the user's context-menu action.

## Staged imports and metadata gate

Direct-to-library downloads are deliberately not supported. A job now has two phases:

1. **Download/stage** - media, metadata sidecars and partial retries remain under `/data/tmp/staging/<collection-id>/`, which is on the persistent `/srv/yt-dlp-server/tmp` host mount.
2. **Validate/publish** - after the job is complete, the server derives one album/collection name and one album artist for the collection, retags the staged audio, copies it into a hidden `.incoming` directory inside `YT-DLP Imports`, then publishes it into `Artist/Album/`. Only after that succeeds is the external staging directory removed. Tag updates use mutagen in place where supported so Opus/Vorbis cover metadata is preserved instead of remuxing every embedded stream through ffmpeg.

The metadata rules are intentionally conservative:

- `album_artist` and `artist` music fields are trusted;
- a YouTube channel ending in ` - Topic` may be used as an artist;
- arbitrary uploaders/channels are **never** used as artists;
- for a playlist, the collection title is preferred over individual videos' album fields, preventing a single playlist from fragmenting into `[Unknown Album]`, `Album - X`, and uploader-specific pseudo-albums;
- multiple legitimate track artists become `Various Artists` at album level while track artists are retained where available;
- exact placeholders such as `[Unknown Album]`, `Unknown Album`, `Unknown Artist`, and `Untitled` are rejected rather than published.

When metadata cannot be resolved, the job shows **Needs metadata** with pre-filled **Artist / album artist** and **Album** fields. Editing those fields pauses automatic card refresh so the form is not overwritten while typing. **Move to library** performs the same retag + publish pipeline using the confirmed values.

## Spotify / spotDL

Spotify URLs are routed through **spotDL** automatically. No separate page or service is required.

For public Spotify track, album, playlist and artist URLs, spotDL retrieves the Spotify metadata, searches YouTube Music first, then YouTube, Bandcamp and SoundCloud as fallbacks, downloads audio only, and embeds the Spotify metadata and cover art. spotDL does **not** download the audio stream from Spotify itself.

Spotify output uses:

    YT-DLP Imports/
      Album Artist/
        Album/
          01 - Track [spotify-track-id].opus

The downloader uses:

- OPUS output;
- `--bitrate disable` to avoid an unnecessary bitrate conversion where possible;
- one spotDL download thread so the server's queue/progress model remains deterministic;
- Spotify album artist, album, track number, title, artwork and other embedded metadata;
- a persistent archive at `/data/state/spotdl-archive.txt`;
- the existing YouTube BgUtils PO-token configuration through spotDL's `--yt-dlp-args` passthrough;
- YouTube Music as the first audio match provider, then YouTube, Bandcamp and SoundCloud as fallbacks;
- no lyrics providers.

The normal **Ignore archive and overwrite existing file** option also applies to Spotify. When unchecked, completed Spotify tracks are recorded in the spotDL archive. A failed partial job can therefore use **Retry automatic search** and only the missing tracks are attempted again.

When every automatic provider fails for a specific Spotify track, the failed job exposes that track separately. Paste a direct YouTube watch URL, Bandcamp track URL or SoundCloud track URL into **Resolve track**. The server sends spotDL its supported manual mapping form, `SOURCE_URL|SPOTIFY_TRACK_URL`, so the chosen source supplies the audio while the original Spotify track still supplies the metadata and cover art. A successful manual resolution is added to the spotDL archive. The original partial album is only published after **all** missing tracks have been resolved and its staged file count matches the expected album size.

spotDL's own processed counter includes failed tracks, so the web UI does not call that value "saved." Saved-track progress is counted separately from successful downloads/existing files, while spotDL's counter is only used to establish the collection size and processing position.

Public Spotify URLs do not require Spotify developer credentials with spotDL's current default metadata client. Authenticated Spotify library shortcuts are outside this web UI's URL-based workflow.

## Playlist preflight

When a valid URL is pasted, the browser asks the server to inspect it before download.

The probe uses yt-dlp in simulation mode with a flat playlist and only the first playlist entry. It does not download media. The result is shown as either:

- `Single item detected - ...`
- `Playlist / album detected - ... - N items`

Playlist/album mode is checked by default. If a URL points to a video that is also part of a playlist, yt-dlp is instructed to use the playlist. Uncheck the option if you only want that individual item.

For YouTube's auto-generated album playlists (`OLAK5uy_...`), the preflight inspects the flat track list. Multiple distinct track artists/channels are treated as a compilation and written with `Album Artist = Various Artists`, while the individual track `Artist` tags are preserved. The playlist title is used consistently as the album title. This prevents Plex from splitting one compilation into separate albums per performer.

A failed probe is advisory: the actual download remains available because some extractors may fail lightweight inspection while still working normally.


## YouTube PO tokens and HTTP 403

YouTube increasingly requires Proof-of-Origin (PO) tokens for some media requests. Without the required token, a video or track can resolve normally and still fail when yt-dlp fetches the actual media with `HTTP Error 403: Forbidden`.

This image bundles `bgutil-ytdlp-pot-provider` **2.0.2** plus its matching BgUtils server scripts. For YouTube URLs only, the app:

- uses the yt-dlp `mweb` player client;
- asks the BgUtils script provider to generate per-video PO tokens automatically;
- runs that provider locally with the already-bundled Deno runtime;
- stores temporary token/cache data under the writable `XDG_CACHE_HOME`;
- leaves non-YouTube extractors unchanged.

The app uses script mode rather than exposing the BgUtils HTTP service. This server has one download worker, so the script-mode concurrency tradeoff is acceptable and there is no additional LAN-facing token-provider port.

The provider path and player client can be overridden if needed:

    BGUTIL_SERVER_HOME=/opt/bgutil-ytdlp-pot-provider/server
    YOUTUBE_PLAYER_CLIENT=mweb

The `/health` endpoint reports whether the provider directory is available and which YouTube client is configured.

A PO token improves compatibility with current YouTube enforcement but cannot guarantee that every YouTube media request will succeed. If YouTube still returns a 403, the job now reports that it failed despite PO-token support instead of showing only a generic yt-dlp exit code.

## Per-version retry history

Every download job records an immutable short software fingerprint based on
the deployed downloader code and tool versions. Retry lineage retains its
overall attempt number. The separate **try N on software X** counter starts at
1 when that fingerprint changes, then increments for retries on the same
software. Historical jobs without a recorded fingerprint are labeled
`legacy`. Automatic HTTP 403 process restarts are counted separately.

## Retry all failed albums

The single **Downloads** section has **Retry all failed** beside the history
filter. Clicking it fetches a server-side preview and asks for confirmation.
It queues **one latest failed/partial attempt per original source**, not one
job per historical failure. Successful, already published, running, queued,
metadata-waiting, manually removed and forced attempts are excluded.
Only sources still present as retryable jobs qualify; the server never tries
to infer an unknown orphan staging folder's source.

Eligible albums are ordered by number of previously downloaded tracks
(descending). Every retry continues its original staging owner, retains the
original files and download archive, and disables forced overwrites.

The action respects the available download-queue capacity. It reports
the number queued and the number not queued. Clicking it again while the
retries are active is idempotent: those sources are excluded automatically.
Newly failed retries can be queued in another batch once the first
attempt has finished.

Endpoints:
- `GET /api/jobs/retry-failed/preview` — dry-run eligible/available counts
  and exclusion breakdown; no modifications.
- `POST /api/jobs/retry-failed` — enqueue up to available capacity, and
  return individual retry IDs and counts; no background scheduling beyond
  the existing single download worker.

This does **not** guarantee YouTube will accept the media requests.
Automatic HTTP 403 recovery and pacing apply to every retried YouTube job.
An earlier successful download, even if it later disappeared from Plex,
must be examined with the disk-to-Plex audit rather than requeued blindly.

## Automatic HTTP 403 recovery and request pacing

YouTube album downloads use the currently recommended `mweb` client with
BgUtils PO-token generation. The app now also avoids repeated rapid requests
and recovers automatically when YouTube rejects a media stream:

1. Pause between YouTube track downloads (default 5-10 seconds), and 1 second
   between extraction requests; yt-dlp HTTP/extractor retries use exponential
   delay. This reduces request pressure but does not eliminate IP restrictions.
2. If a process exits nonzero after an actual HTTP 403, wait 30 seconds and
   restart yt-dlp to **re-extract a fresh stream URL**, keeping staged audio
   and preserving completed tracks via the download archive.
3. If the second attempt also fails with a 403, wait 90 seconds and make a
   final attempt with yt-dlp's maintained `default` player-client selection.
   This avoids assuming the older `web_safari` HLS path is available.
4. Record the exact number of automatic restarts; show a recovering state
   during backoff, and keep the job failed with its staging files untouched
   if all attempts fail. A later manual retry remains possible.

These retries are bounded. They do not run for Spotify, non-YouTube sources,
non-403 failures, or explicitly forced/overwriting jobs. YouTube downloads
with many tracks are expected to run slower than before.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `YOUTUBE_403_RESTARTS` | `2` | Fresh yt-dlp process restarts (0-3) |
| `YOUTUBE_403_BACKOFF_SECONDS` | `30` | Initial cooldown; second is 3× (5-300) |
| `YOUTUBE_403_FALLBACK_CLIENT` | `default` | Client for final restart (empty = keep mweb) |
| `YOUTUBE_TRACK_PAUSE_MIN` | `5` | Minimum per-track delay (0-30 seconds) |
| `YOUTUBE_TRACK_PAUSE_MAX` | `10` | Maximum per-track delay (0-45 seconds) |
| `YOUTUBE_REQUEST_PAUSE` | `1` | Delay between extraction requests (0-10 seconds) |

**Limitations:** A PO-token provider directory existing does not establish
that a valid token was generated or accepted. The revised error message
states this explicitly. Run yt-dlp with `-v` for a single failing item to
verify both the detected provider and a line indicating actual token
generation; do not share cookies, raw tokens or signed stream URLs in
public logs. A generated PO token cannot guarantee avoidance of HTTP 403,
and IP-based restrictions may persist. The original staging data is
retained when automatic recovery fails.

## Unavailable YouTube playlist tracks

YouTube playlists can contain hidden or removed videos. yt-dlp normally continues downloading the rest of the playlist, but still exits nonzero when an unavailable video is encountered. The server detects errors such as:

    ERROR: [youtube] riu2Bx6miIU: Video unavailable

and exposes the unavailable video as a missing track on the failed job.

The job card offers **Ignore permanently**. Ignored YouTube video IDs are stored at:

    /data/state/ignored-tracks.json

Ignoring does not fabricate or count a file as downloaded. For an ordinary playlist a 13/14 result may be closed with `1 ignored`, but **an album remains incomplete and unpublished** until all 14 tracks are actually present. Future runs may still encounter the unavailable entry; ignoring it does not override the album-completeness gate.

Retry remains available until the missing track is ignored, so temporary availability failures can still be retried instead.

## Repairing imports created by older versions

Versions before the staging pipeline may already have created uploader folders, duplicate pseudo-albums, or `[Unknown Album]` entries. The image includes a one-time repair tool that uses the persisted job URLs and source IDs to rebuild those imports.

First run a **dry run**:

    docker compose exec yt-dlp-server \
      python /app/tools/repair_legacy_imports.py

It probes the original YouTube/YouTube Music URLs again, groups the files by the original collection, prints the intended `Artist/Album` destination, and reports anything it cannot resolve. Album repair deliberately performs a **full per-track metadata probe** rather than yt-dlp's flat-playlist probe: this is slower, but it recovers `album_artist`, `artist`, and stable `Artist - Topic` channel metadata that flat playlist data often omits. Successful full probes are cached under `/data/state/repair-metadata-cache/`, so the later `--apply` reuses the exact metadata reviewed in the dry run instead of querying every track again. Add `--refresh-metadata` if the source has changed and you intentionally want to rebuild that cache. It does not modify music files without `--apply`.

After reviewing the plan:

    docker compose exec yt-dlp-server \
      python /app/tools/repair_legacy_imports.py --apply

For each collection, the repair tool first copies and retags all replacement files into a hidden incoming directory. Only after the whole replacement collection is prepared successfully does it publish the corrected album and remove the old scattered copies. Arbitrary uploader names are never used as artists here either.

To repair only one exact persisted source URL, add:

    --url 'https://music.youtube.com/playlist?list=...'

Legacy standalone YouTube/YouTube Music watch URLs are **skipped by default**. The repair command is album-focused and will not turn an old one-track download into a one-file album directory. Add `--include-singles` only if you explicitly want those old single-video jobs considered too.

Artist inference is conservative: a value must be supported by a majority of the fully inspected tracks. Explicit `album_artist` wins, followed by a stable `Artist - Topic` channel, followed by one consistent per-track artist. Playlist-level generic values are never trusted as the artist.

## Idempotent album submissions from the Plex Playlist Manager

The downloader exposes a dedicated JSON API alongside the existing HTML form:

    POST /api/albums
    Content-Type: application/json

    {
      "url": "https://music.youtube.com/playlist?list=OLAK5uy_...",
      "match_audit": {
        "requested_artist": "Miles Davis",
        "requested_album": "At the Blackhawk, Complete",
        "matched_artist": "Miles Davis",
        "matched_album": "At the Blackhawk - Complete (Live)",
        "match_type": "explicit_alias",
        "normalization_rules": ["explicit_album_alias"],
        "queries": ["Miles Davis At the Blackhawk, Complete"]
      }
    }

The server validates the **YouTube Music album ID** and computes its own
stable idempotency key from the ID. The first request returns HTTP 201 with
`ok: true`, `duplicate: false`, `job_id` and `status: "queued"`.
Repeated submissions return HTTP 200 with `duplicate: true` and the original
`job_id`, without adding a job. This is atomic under the job lock, including
simultaneous requests. The legacy HTML `POST /download` shares the same
album identity check when its playlist option is enabled.

Claims are persisted with `jobs.json` and survive restarts and visible-history
pruning. The last-known job status and bounded match audit are retained when
the old job itself has been pruned. A repeat POST for a **visible job**
includes `visible: true` and its actual state. A completed job pruned from
history returns `visible: false`; it is never represented as a queued job.
An orphaned interrupted/failed/removed claim allows a new request to be
queued instead of permanently blocking downloads. The album status endpoint
returns current visibility, job status and the latest retry ID. The browser
dashboard supports `?job=<id>`: it highlights the corresponding card or
reports clearly when the requested job is absent on that server. This helps
detect an incorrect `YTDLP_PUBLIC_URL` pointing to a different instance. Query
`GET /api/albums/<job_id>` for status, source match rules and candidate
rankings when available; `GET /api/jobs` still lists visible UI jobs.

HTTP 429 means the queue is full; HTTP 503 means the job could not be
persisted. The server never acknowledges a new job before its state has been
written. A retry after a network timeout is safe because the album key is
derived from the URL, not a client-generated random request ID.

The API is for verified album URLs (`OLAK5uy_...` playlist links or
`MPRE...` browse links); it does not accept YouTube Music search pages,
videos or arbitrary external URLs. If the service receives an `MPRE` URL
and its equivalent `OLAK` URL separately, they remain two different IDs;
the playlist manager prefers `OLAK` URLs when available.

The downloader has **no login** and is intended for a trusted LAN only.
Do not expose the album API to the public internet.

## Understand download status and duplicates

The **Downloads** overview shows working, waiting, metadata-needed,
failed/partial, and finished jobs, plus the number of repeated album requests
prevented. Each job has a plain-language **outcome** and **next action**, using
the job's actual state and saved evidence. This avoids mistaking a process
marked `running` for confirmed audio progress:

- **Reading source playlist** means yt-dlp is running, but no tracks have
  been confirmed; a YouTube Music → YouTube playlist redirect is normal.
- **Downloading tracks** means actual files were recorded, with confirmed
  saved/total counts when known.
- **Action needed: album metadata** means files remain staged until approved.
- **Album published** appears only after a library publication directory
  has been recorded.
- **Finished without new files** does *not* claim a successful publication.
  Confirmed yt-dlp archive skips are counted and explained separately.
- **Duplicate download prevented** refers to repeated requests for the
  **same YouTube album ID**. These reuse the original job, with the count of
  suppressed extra requests visible on its download card.

The original artist and album query, matched catalog title and applied rule
also appear on the job card when the request came from the Plex Playlist
Manager. You can still expand **Log** for raw diagnostic output. During a
running job, the card reports how long it has received no new yt-dlp output;
silence is not itself proof that the job has failed.

An album already present in Plex is rejected by the *playlist manager* before
submission. A yt-dlp **archive skip** indicates that a source item was recorded
in the downloader's archive; it does not guarantee Plex currently contains
that file. Neither is conflated with a duplicate job request.

## Permanent download history and disk-first inventory

The server no longer prunes failed or completed download attempts after 50
jobs. All attempts remain in persistent `/data/state/jobs.json` (including
failures and their last error log). If a job card is explicitly removed from
the active list, its record moves to `archived_jobs` inside that state file.
Neither a successful nor a failed attempt disappears from
`GET /api/download-history`, which supports `q`, `status`,
`offset`, and `limit` filters. The dashboard has **one Downloads section**. Queued, running and
metadata-waiting jobs use full interactive cards. Finished and failed jobs
appear exactly once as compact rows below; their log and recovery controls
can be inspected through a focused job link when necessary. All retained
results load automatically, with failures first. Only explicit retry chains
are collapsed into one result, and earlier attempts remain expandable.
Independent downloads are never merged by guessed album names. The search
box and status filter are optional, and Ctrl+F works on the rendered
results. A failed job without a later retry has a Retry button.
An HTTP 403 error is recorded immediately and remains visible.
`MAX_HISTORY` is deprecated and ignored; it cannot silently delete failures.

`GET /api/disk-inventory` scans **all of `MUSIC_ROOT`**, independently of
download jobs, and groups audio files by their actual disk album folder.
Albums under `IMPORT_SUBDIR` use the Artist/Album folder as a group, even
when there are nested disc subfolders. Other music folders are grouped by
their immediate parent directory. It separately enumerates unpublished audio
under `TEMP_DIR/staging`. It does not follow symlink folders or hidden
publish-in-progress folders. A scan that hits a filesystem error or safety
limit returns `complete: false` with an error, rather than silently claiming
all files were checked.

The Plex Playlist Manager's **Downloadcontrole** page compares these disk
files to Plex's indexed media file paths, ignoring composer and performer
tags for the match. A file that never downloaded (for example, after HTTP
403) cannot appear in the disk inventory; the separate permanent history
records the failed attempt instead. A confirmed file on disk but absent from
Plex still requires diagnosis of Plex library mounts, scans, access rights,
or unsupported media. Requesting a Plex scan is not proof that indexing
succeeded.

Already-pruned or deleted attempts from **older versions** cannot be
reconstructed from the new history. The disk inventory can still locate
files left on disk; the yt-dlp archive may also contain successful source
IDs, but it does not establish that Plex indexed the album.

## Removing finished or abandoned jobs

Terminal job cards can be removed from the UI:

- **Discard** on failed or metadata-waiting jobs removes the job and cleans its unpublished staging directory when no retry/resolution still shares that staging area.
- **Remove from list** on succeeded jobs hides the active card but keeps its permanent history record. Published music files are never deleted.
- Queued/running jobs cannot be removed through this action while work is active.

This is useful for album-level failures that are not worth resolving track-by-track, such as a Spotify album where every spotDL match failed.

## Persistent queue and restart recovery

Job history and queued work are persisted to:

    /data/state/jobs.json

The state file is updated atomically when jobs are created, retried, manually resolved, started, completed, fail, or save another track. Because `/data/state` is a persistent volume, queued jobs and the visible job history survive container rebuilds/restarts.

Restart behavior is deliberately conservative:

- jobs that were **queued** are automatically requeued after restart;
- jobs that were **running** are restored as failed with `Interrupted by server restart - use Retry / continue`;
- completed and failed job history remains visible;
- an interrupted job is not silently restarted, which avoids accidentally repeating a forced/overwrite download.

### Migrating the current in-memory queue

Versions before persistent queue support only keep jobs in memory. Before the first upgrade to a version with queue persistence, save the current `/api/jobs` response directly into the persistent state directory:

    curl -fsS http://127.0.0.1:4545/api/jobs \
      -o /srv/yt-dlp-server/state/jobs.json

The new server accepts this older JSON-array format on first startup, normalizes it into the persistent state format, restores queued jobs, and preserves the rest of the visible history.

After this migration, no manual queue export is required for future updates.

## Duplicate handling

Normal yt-dlp downloads use `/data/state/archive.txt`. Spotify/spotDL downloads use `/data/state/spotdl-archive.txt`. Each archive records successfully completed source items so retries and repeated submissions can skip tracks that are already complete.

The **Ignore archive and overwrite existing file** checkbox deliberately bypasses that protection.

## Cookies

Some sites may require authentication. Export a Netscape-format `cookies.txt`, place it at:

    /srv/yt-dlp-server/state/cookies.txt

and add:

    COOKIES_FILE: /data/state/cookies.txt

to the service environment. Treat the cookie file as a secret.

## Updating yt-dlp and spotDL

yt-dlp changes often because upstream sites change. The image intentionally installs the current PyPI yt-dlp release at build time rather than pinning an old extractor release. spotDL is pinned to the stable 4.6.0 release; this update also excludes broken `ytmusicapi` 1.12.2 and 1.12.3 releases. The app's CLI/progress integration still requires runtime smoke verification after rebuilding.

Rebuild when downloads start failing:

    git pull
    docker compose build --pull --no-cache yt-dlp-server
    docker compose up -d yt-dlp-server

Watchtower does not rebuild locally built images, so it cannot update yt-dlp inside this service by itself.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `4545` | HTTP port inside the container |
| `MUSIC_ROOT` | `/data/music` | Mounted Plex music root |
| `IMPORT_SUBDIR` | `YT-DLP Imports` | Isolated directory inside the music root |
| `STATE_DIR` | `/data/state` | Persistent archives, queue and job history |
| `TEMP_DIR` | `/data/tmp` | Persistent staging and temporary files; keep this outside `MUSIC_ROOT` |
| `COOKIES_FILE` | empty | Optional Netscape-format cookies file |
| `MAX_QUEUE` | `50` | Maximum waiting jobs |
| `MAX_HISTORY` | ignored | Deprecated: history is now permanent; this variable cannot prune failed downloads |
| `PROBE_TIMEOUT` | `30` | Maximum link-inspection time in seconds |
| `BGUTIL_SERVER_HOME` | `/opt/bgutil-ytdlp-pot-provider/server` | Bundled BgUtils provider scripts |
| `YOUTUBE_PLAYER_CLIENT` | `mweb` | YouTube client used with automatic PO tokens |

## Security

This interface has no login and is intended for a trusted LAN. Do not expose port 4545 directly to the public internet.
