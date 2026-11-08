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

## Install and update

**The downloader and managed WireGuard controller now run in one Docker
container**, `yt-dlp-server`. A root-owned controller manages wg0 and
iptables; the web server, yt-dlp and spotDL run as the configured non-root
UID/GID. The control API binds to loopback port 8787, while the existing
dashboard remains on port 4545.

The default connection is direct. WireGuard activates only when the
existing bot-challenge/403 recovery policy requests it or when a profile
is explicitly connected in the dashboard.

For a new standalone deployment:

```sh
docker compose config --quiet
docker compose up -d --build yt-dlp-server
```

### Migrating the existing `~/media-stack`

The existing `~/media-stack/docker-compose.override.yml` symlink to
`yt-dlp-server/compose.parent-vpn.yaml` **must remain**. Its target now
augments only the downloader service, without defining a separate
gateway service. The parent's `yt-dlp-server` network name and port
4545 are preserved for the playlist manager and other clients.

**Run this once, only when no download is running or queued.**
Confirm this in the Downloads page before stopping containers.

```sh
cd ~/media-stack/yt-dlp-server
git pull --ff-only
cd ..
docker compose config --quiet
docker compose build yt-dlp-server
docker stop yt-dlp-server
if docker container inspect yt-dlp-wireguard-gateway >/dev/null 2>&1; then
    docker rm -f yt-dlp-wireguard-gateway
fi
docker compose up -d --no-build --force-recreate yt-dlp-server
docker compose ps
```

Removing the former `yt-dlp-wireguard-gateway` container is necessary:
it previously owned host port 4545. The replacement service publishes
this port itself. **Do not run `docker compose down -v` or
`docker volume prune`.** Music, cookies, state, archives, staging and
root-owned WireGuard profiles retain their existing bind-mount paths.
The old container is not needed after this migration.

Subsequent updates need only one rebuild:

```sh
cd ~/media-stack/yt-dlp-server
git pull --ff-only
cd ..
docker compose config --quiet
docker compose up -d --build --force-recreate yt-dlp-server
```

Inspect both processes through the single container:

```sh
docker compose ps yt-dlp-server
docker compose exec yt-dlp-server ps -eo pid,user,args
docker compose logs --tail=80 yt-dlp-server
```

The image includes the existing yt-dlp, spotDL, ffmpeg, Deno and
PO-token dependencies alongside WireGuard tools and iptables. Its PID 1
supervisor starts the gateway first, starts Gunicorn through `gosu`
as `VPN_DOWNLOADER_UID:VPN_DOWNLOADER_GID` (default `1000:1000`),
and drops NET_ADMIN from the downloader capability bounding set with
`setpriv --no-new-privs`. If either process fails, the supervisor stops
the other and Docker restarts the whole container. The container requires `NET_ADMIN`, `/dev/net/tun` and
`src_valid_mark` but **Gunicorn and its download workers run without
root privileges**.

## Surfshark VPN: upload and test entirely in the web interface

After the normal downloader update above:

1. Open the downloader at **port 4545** and expand **Surfshark VPN**.
2. Download Surfshark **WireGuard** `.conf` configurations for your desired
   locations (Surfshark account > VPN > Manual setup > WireGuard).
3. Select several `.conf` files in the upload field and choose
   **Upload .conf files**. No administrator token or country text field is
   required. **If Surfshark provided templates**, they contain the literal
   `PrivateKey = <insert_your_private_key_here>`. The page detects this and
   asks for your *registered WireGuard client private key once*, filling it
   into every selected template. Obtain the key from Surfshark account >
   VPN > Manual setup > WireGuard > Generate a key pair, or retrieve the
   private key you securely saved when originally generating one. A random
   unregistered key will not connect. If you lose the private key, generate
   a new key pair in Surfshark and use its new private key.
   Profiles are persisted under
   `/srv/yt-dlp-server/wireguard-profiles` and never shown with private
   keys. Country codes are inferred from location filenames
   (for example, `nl-ams.conf`, `de-fra.conf`).
4. Uploading profiles does **not** connect the VPN. They remain available
   for automatic recovery after the specific YouTube failures described below.
   Select **Test all countries once** to verify each location's public IP
   and country; this temporary test ends in the normal direct mode.
5. Use **Try VPN endpoint** or a profile's **Connect** button for an
   explicit manual connection. Use **Disconnect VPN** to return to direct
   immediately. Starting the next download always returns to direct first.

### Dashboard access when the VPN handshake fails

The combined container publishes `4545/tcp`. Its gateway marks
**only TCP replies from source port 4545** with WireGuard's
`FwMark=51820` so dashboard traffic uses Docker's original underlay
route while the VPN is active. Download traffic remains subject to
the outbound-DROP kill switch.

Verify from another machine with
`curl -4 --connect-timeout 5 http://media-server:4545/health`.
Rebuild the **single** `yt-dlp-server` container to apply VPN-network
changes. There is no need to re-upload profiles.

### Client key display

The VPN panel shows the client public key used by each uploaded location.
All countries may share one registered client key; Surfshark's server keys
are distinct and must not be registered as client keys. Expand any saved
profile's Client public key section to compare it with Surfshark settings.

### Diagnosing a failed connection

If all countries fail with the same result, keep the uploaded profiles;
do not upload or share your private key again. Rebuild the combined downloader container to get specific,
non-sensitive error messages:
`permissions` (NET_ADMIN), `kernel` (WireGuard unavailable),
`tunnel_setup` (wg-quick/routing), `handshake` (no reply from
Surfshark; key registration or UDP reachability), `dns` (name resolution
failed after a successful handshake), and `exit_ip` (HTTPS verification
failed after a successful handshake and DNS).

The test checks for a real WireGuard handshake before looking up the
public exit IP, and can fall back to a second HTTPS geolocation service.
If VPN setup fails, the dashboard shows **VPN NOT CONNECTED · DOWNLOADS
BLOCKED** while its kill switch is engaged. Direct mode is reported
separately; an absent active profile alone is not proof of direct access.

**All downloads start on the normal direct Docker/ISP connection, even when
Surfshark profiles are already uploaded.** The gateway returns to direct
mode after container restarts; it does not reconnect an old profile.
Uploading profiles only prepares standby recovery endpoints.
Your regular public IP is visible to YouTube on each first attempt.

Temporary VPN fallback is enabled by default. An explicit
`Sign in to confirm you're not a bot` error triggers a VPN connection only
after the initial yt-dlp process exits. A repeated HTTP 403 triggers VPN
only on the final permitted retry, because HTTP 403 can also indicate a
media stream or PO-token problem. The gateway checks the new public exit IP
and skips failed tunnels or exits that retain the previous public IP.
Up to two additional processes are attempted by default.

**After every download job, WireGuard disconnects and the downloader
returns to its regular direct connection before the next job starts.**
This applies to successful downloads, failed retries, and subprocess errors.
If the direct route cannot be restored, the kill switch remains engaged
and the job reports the network failure. Existing staged files are retained.

The dashboard shows **DIRECT CONNECTION · DEFAULT** when idle.
**Connect** and **Try VPN endpoint** are explicit manual overrides;
**Disconnect VPN** switches back immediately. Starting a new download
always restores direct mode first. **Test all countries once** temporarily
connects to each profile and then returns to direct mode.

The kill switch protects traffic while WireGuard is active or changing.
Direct mode intentionally exposes the regular public IP. YouTube may
challenge a login session when it suddenly appears from a different
VPN location; a VPN is not guaranteed to improve access.

To disable automatic VPN rotation for both failure types, set
`SURFSHARK_ROTATE_ON_403=0` in
the parent Compose project's `.env` (or the downloader environment), then
recreate `yt-dlp-server`. Any existing explicit `0` overrides the
default-on setting.

**Security:** The control API binds to loopback inside the
combined downloader container's network namespace and authenticates with a private
automatically generated token stored under the persistent downloader
state directory. Uploaded WireGuard keys are stored only in the
root-owned private `/profiles` mount. For template files, the browser
**does send your pasted client private key to the downloader backend**
as part of the local upload request. It is not placed in logs or saved
in browser storage, but HTTP traffic can be observed on untrusted networks;
use HTTPS or a trusted private LAN for this upload. Never paste the key
into a chat or commit it to Git.
Since the main downloader web UI has no user login, everyone who can
access it can also submit VPN management actions; keep port 4545
restricted to your trusted LAN or behind authenticated HTTPS.
Cross-origin browser form submissions are rejected. The managed gateway
is not yet validated against your own Docker host or independently
DNS-leak-tested. Docker's resolver may forward DNS queries outside the
VPN; do not assume this is a privacy-audited network configuration.

Obsolete two-container Gluetun and WireGuard Compose overrides and
the separate gateway Dockerfile have been removed. The managed,
single-container deployment is now canonical; historical variants
remain available in Git history.

## Chrome extension

The running web app builds the extension ZIP directly from the
canonical `chrome-extension/` source files when you click **Chrome
extension**. Download the current package from `/chrome-extension.zip`
on your media server. There is no standalone packaging script or prebuilt
ZIP dependency inside Docker.

The older v1.0.0 ZIP remains in the repository as a historical release
snapshot, but production packages now come from the current sources.

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

## Automatic YouTube 403/bot verification recovery and request pacing

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
4. Start each job on the direct connection. For a bot challenge, connect
   to a verified Surfshark exit before the first retry. If another bot
   challenge occurs, switch to a different VPN exit where available.
5. A repeated HTTP 403 can use the VPN only for the final permitted
   attempt. Nonrecoverable errors and Spotify jobs do not activate it.
6. Disconnect WireGuard and restore the direct connection before
   publication or the next job. On failed restoration, stop and retain
   staged files. Record bot/403 restarts and successful VPN switches.
These retries are bounded. They do not run for Spotify, non-YouTube sources,
failures other than HTTP 403 or explicit bot verification, or forced/overwriting
jobs. The normal standalone `docker exec yt-dlp-server yt-dlp ...` command does
not use the app's queue and therefore does not trigger automatic rotation.
YouTube downloads
with many tracks are expected to run slower than before.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `YOUTUBE_403_RESTARTS` | `2` | Fresh yt-dlp process restarts (0-3) |
| `YOUTUBE_403_BACKOFF_SECONDS` | `30` | Initial cooldown; second is 3× (5-300) |
| `YOUTUBE_403_FALLBACK_CLIENT` | `default` | Client for final restart (empty = keep mweb) |
| `YOUTUBE_TRACK_PAUSE_MIN` | `5` | Minimum per-track delay (0-30 seconds) |
| `YOUTUBE_TRACK_PAUSE_MAX` | `10` | Maximum per-track delay (0-45 seconds) |
| `YOUTUBE_REQUEST_PAUSE` | `1` | Delay between extraction requests (0-10 seconds) |

**Limitations:** Switching authenticated YouTube cookies between your regular
IP and a Surfshark exit may itself trigger a new bot challenge. Verify
cookies through the web interface if necessary. A PO-token provider
directory existing does not establish
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

    docker compose exec -u 1000:1000 yt-dlp-server \
      python -m app repair-library

It probes the original YouTube/YouTube Music URLs again, groups the files by the original collection, prints the intended `Artist/Album` destination, and reports anything it cannot resolve. Album repair deliberately performs a **full per-track metadata probe** rather than yt-dlp's flat-playlist probe: this is slower, but it recovers `album_artist`, `artist`, and stable `Artist - Topic` channel metadata that flat playlist data often omits. Successful full probes are cached under `/data/state/repair-metadata-cache/`, so the later `--apply` reuses the exact metadata reviewed in the dry run instead of querying every track again. Add `--refresh-metadata` if the source has changed and you intentionally want to rebuild that cache. It does not modify music files without `--apply`.

After reviewing the plan:

    docker compose exec -u 1000:1000 yt-dlp-server \
      python -m app repair-library --apply

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

Open the **YouTube cookies** panel in the downloader webinterface on port 4545.
Export cookies for youtube.com from your browser in Netscape/Mozilla `cookies.txt` format (not JSON), select the file, then click **Upload cookies**.
The app validates the file, retains only YouTube cookie entries, and stores the result in `/data/state/cookies.txt` with `0600` permissions. The state volume persists between rebuilds.
All yt-dlp YouTube download, probe and retry commands and spotDL commands reuse that one configured cookie file. Remove it with the button in the same panel.
Uploads and removal are blocked while jobs are queued or running. Forms use a random server-side token to prevent cross-site submissions; no password or login is required on this single-user LAN installation.
If `COOKIES_FILE` is explicitly set, the upload replaces that configured path instead. Do not expose port 4545 to the public internet.
Use **Verify cookies** in the same panel after uploading. This runs a two-stage online check: an account-page request to determine whether YouTube recognizes the signed-in session, followed by a bounded, metadata-only yt-dlp extraction of a public test video with the same cookie file and extractor settings as downloads. No media is downloaded. Results are **Valid**, **Rejected**, or **Inconclusive**, with a specific explanation (including VPN/IP blocks). A public video alone is never treated as proof of authentication, and no cookie values or account details are exposed in the response. The verification does not alter the saved cookie file; it uses a temporary private copy. The tool does not guarantee subsequent video downloads will work.
Cookies can expire, and YouTube may still require further verification. yt-dlp recommends exporting YouTube cookies from a dedicated private browsing session to avoid rotation; using an account for automated downloads can trigger account restrictions.

### Diagnosing incomplete albums

A source probe reporting 12 tracks does not guarantee that the actual yt-dlp
download enumerated 12 playable videos. For incomplete albums, the dashboard
now retains the original yt-dlp exit code, the highest playlist position
reached, the number of global archive skips, the last extractor error, and
the number of staged audio files. The **failure reason** is shown separately
from the publication safety rule; it persists even when the rolling 40-line
log drops older messages.

Typical classifications include **no downloadable playlist entries**,
**already recorded in the global archive but missing from staging**,
**YouTube bot/HTTP 403**, **extractor error**, and **item reached without
downloaded audio**. In particular, a normal YouTube Music-to-YouTube redirect
is not diagnosed as a failure by itself. Retries retain staged tracks but
do not silently delete or bypass the global download archive. If a
previously downloaded album exists elsewhere in the music library, inspect
it before deliberately overriding archive protection.

The real download command explicitly includes `--no-simulate` and
`--no-quiet` to prevent `--print` telemetry from silently simulating
or suppressing diagnostic progress. No video/audio content is generated
during the source probe or cookie verification.

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
| `COOKIES_FILE` | `/data/state/cookies.txt` | Optional override for the web-uploaded YouTube cookies file |
| `MAX_QUEUE` | `50` | Maximum waiting jobs |
| `MAX_HISTORY` | ignored | Deprecated: history is now permanent; this variable cannot prune failed downloads |
| `PROBE_TIMEOUT` | `30` | Maximum link-inspection time in seconds |
| `BGUTIL_SERVER_HOME` | `/opt/bgutil-ytdlp-pot-provider/server` | Bundled BgUtils provider scripts |
| `YOUTUBE_PLAYER_CLIENT` | `mweb` | YouTube client used with automatic PO tokens |

## Security

This interface has no login and is intended for a trusted LAN. Do not expose port 4545 directly to the public internet.


## Standalone script policy

The `scripts/` and `tools/` launchers have been removed. Docker Compose
is the deployment mechanism; the Flask application serves its own current
Chrome extension ZIP. The existing `app.py` entrypoint also exposes
`python -m app repair-library` for historical data repair, including the
original dry-run-first and explicit `--apply`, `--url`,
`--include-singles`, `--refresh-metadata`, `--music-root`,
`--import-subdir`, and `--state-dir` options. Because the container supervisor itself runs as root, execute repair
commands using `docker compose exec -u 1000:1000` (or your configured
downloader UID/GID). In maintenance mode download workers are not
started, queued jobs are not resumed, and no server port is opened.

Original scripts and the historical ZIP remain recoverable in Git history.
The cleanup does not delete music, WireGuard profiles, job history, archives,
staging files or cached metadata.
