# Plex Playlist Manager + yt-dlp

One Git repository for the Plex Playlist Manager and the complete yt-dlp
download service, with both original commit histories preserved in a
non-squashed Git subtree merge. The two applications remain separate
Docker containers, with one browser interface on port 5051.

The repository structure is:

- `app/`, `tests/`, `Dockerfile`: Playlist Manager
- `yt-dlp-server/`: full yt-dlp engine and privileged WireGuard controller
- `deploy/`: one-checkout media-stack migration and safe deployment
- `compose.yaml`: coordinated two-service standalone Compose stack
- `.github/workflows/`: unified application checks and downloader Docker CI

This application defines Plex music playlists in YAML, previews matches
against the Plex Music library and syncs them idempotently.

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

## One navigation and two entry workflows

Open `http://media-server:5051/`. Home is a functional entry point, with
two prominent actions:

1. **Import playlist YAML** — select or drop a `.yml` / `.yaml` file
   (maximum 2 MB). The app validates it first. Valid files are then merged
   into the saved collection and the changed playlists are automatically
   synced to Plex, with **no checkbox or second confirmation**. Unmentioned
   playlists and unrelated pending changes are preserved. The interface
   shows new/updated/unchanged/retained counts, progress and per-playlist
   failures. Invalid YAML never changes the saved collection; a Plex outage
   leaves the validated imported definitions saved for retry. The validation
   token binds the exact file bytes to the current collection revision.
2. **Download music** — paste a source URL, optionally choose album/playlist
   and force-download settings, and submit directly to the internal
   downloader. The resulting job opens in `/downloads`.

Below the entry forms, Home shows live Plex, saved-playlist, download and
pending-sync counts, followed by specific issues requiring attention. The
read-only status updates automatically every 30 seconds when the tab is visible
and immediately after a completed YAML import. No refresh button is required.

**One shared navigation and theme** is used on every screen, including the
native downloader. The header has Home, Downloads and Playlists & library.
The playlist dropdown opens creation, curation, discovery, reports and
audio analysis; Tools & exports contains diagnostics and exports.
There are no separate page headers, second playlist tab bars, drawers or
embedded frames. The mobile header collapses the same navigation into one
Menu button.

The playlist editor remains at `/playlists`. Its library sections have
direct URLs such as `/playlists?view=health`,
`/playlists?view=newmusic`, `/playlists?view=isc` and
`/playlists?view=duplicates`. Existing `/?view=...` bookmarks work.
The advanced YAML editor, cover ZIP upload and playlist-preview remain in the
playlist workspace. Manual proposal publishing and generator drafts still
have their separate review controls.

The YAML configuration API does not require Plex connectivity. If Plex or
the downloader is unavailable, their status is shown independently; the
other tools and saved playlist definitions remain accessible.

## Appearance: Cyber, Slate, and Terminal

Use **Tools & exports → Appearance** in the single site navigation to choose
**Cyber** (original mint neon), **Slate** (muted dark), or **Terminal**
(hacker-green text, dark CRT-style grid, squared panels and monospace
headings). The selection applies across Home, Playlists, Downloads, album
diagnostics and analysis pages and persists in this browser. Terminal uses
a static brand mark and has a reduced-motion override. Changing themes
does not reload data or restart the downloader.

## Automatic workspace maintenance and faster loading

The application has one global navigation (**Home**, **Downloads**, **Playlists & library**).
The playlist workspace at `/playlists` contains the original library, generator,
curator, discovery, reports and audio-analysis workflows, with one shared workflow
selector and one shared visual system. Direct bookmarks to the older standalone
routes still work. This reuses the existing engines and publication safeguards.

Three server workers run independently of the browser:

- **AudioMuse feature cache:** sync completed analysis every 15 minutes, up to
  500 changed tracks per batch, continuing at five-second intervals while pending.
- **Plex music catalogue:** refresh the full track/album index every 15 minutes.
  Browsers read the most recent complete snapshot without waiting for a scan.
- **New-music analysis:** every two minutes, check the durable queue of newly
  indexed Plex tracks (including new tracks on existing albums). When an existing Plex index is available, it is used as the initial
  baseline before the next complete scan, so arrivals during downtime are
  captured. Otherwise the first successful scan establishes the baseline.
  Later additions enqueue their albums. Check upstream AudioMuse task state before submitting a bounded
  `num_recent_albums` analysis through the **existing** AudioMuse API. Only
  one task may be in flight. Task IDs and pending albums are persisted in
  `/data/workspace-analysis.json`, including across restarts. An analysis that
  fails is retried at most three times; missing or ambiguous completion evidence
  is **reported**, not silently treated as success. Full-library backfill is
  available in the advanced audio-analysis controls.

The first baseline intentionally does not initiate a potentially expensive
full-library analysis. If old music is still unanalyzed, run an explicit backfill.
Automatic requests use AudioMuse's recent-albums API, so its recent-album ordering
must correspond to the same Plex library; the actual analyzed coverage is exposed
separately. Neither these workers nor the existing downloader-to-Plex scan can
publish or replace playlists or YAML definitions. Validated YAML uploads
automatically synchronize their changed playlists; manual curation proposals
and generated drafts retain their explicit review controls. Enable the existing downloader scan using
`DOWNLOAD_BRIDGE_SCAN_ENABLED=1` in the parent Compose configuration.

`GET /api/workspace/maintenance` reports queue depth, active analysis,
last successful cache/index updates and failures. Disable all maintenance with
`WORKSPACE_AUTO_REFRESH_ENABLED=0`, or only automatic analysis with
`WORKSPACE_AUTO_ANALYSIS_ENABLED=0`.

**Loading improvements:** Home requests the lightweight local
`/api/config/summary` endpoint instead of fetching full YAML, and displays
each independent status as soon as its response arrives. A slow Plex or
downloader request cannot hold up local playlist counts. Full Plex scans
are handled by the background indexer and never block an ordinary cached
library read. Repeated requests share a decoded AudioMuse SQLite snapshot
until its file changes or a sync commits. The playlist-creation readiness
and genre evidence endpoints now read that local catalogue and reuse normalized
audio embeddings without opening a new Plex connection or waiting for a live
AudioMuse task-status response. AudioMuse task progress has a separate endpoint.
These improvements preserve the last successfully fetched catalogue on
transient outages.

The playlist editor also checks `GET /api/config/status` every 90 seconds
when visible and idle, updating external changes without losing unsaved YAML
edits, filters or expanded rows. Report and analysis views refresh themselves.

## Playlist collaboration export and proposal import

Open **Playlists & library** (`/playlists`), then select
**Export for ChatGPT (ZIP)**. This is a read-only snapshot intended for
working with an assistant on track selection, analysis and ordering.

The ZIP contains four UTF-8 files:

| File | Contents |
| --- | --- |
| `tracks.csv` | Every currently indexed Plex track ID and artist, album, title, year, genre, disc/track and duration; enriched Plex Track XML attributes, moods, tags and media codec/bitrate data in `plex_metadata_json` (no filesystem path); known AudioMuse tempo, energy, musical key, scale, mood vector and **all non-embedding raw analysis fields** in `audio_analysis_raw_json`. Every row marks missing, partial, complete or invalid embeddings. |
| `embeddings.jsonl` | One JSON line per Plex track **plus** orphan AudioMuse records. Full, *unnormalized* float32 MusiCNN and CLAP vectors, exact original base64 payloads, complete raw API record, each vector's availability/validity, and an `in_plex_library` flag. No embedding dimensions or extra analysis measurements are truncated. |
| `playlists.yml` | Existing saved YAML definitions under `playlists`, alongside actual live Plex playlist order under `live_plex_playlists` when Plex is reachable. These can differ. A live Plex outage does not invent playlist ordering; the missing snapshot is declared in the manifest. |
| `manifest.json` | Timestamp, snapshot revisions, cached-analysis provenance and complete coverage/missing-data counts. This is the authority for proposal source IDs. |

The complete track catalogue comes from the last successfully indexed Plex
snapshot (normally updated every 15 minutes). Its age appears in the
manifest. Legacy snapshots that lack enriched Plex metadata are counted
explicitly and filled at the next successful background rescan. Live
playlist order is fetched during export when available.
The background AudioMuse sync now stores *every* field returned by its
sync API, instead of discarding unknown fields. On first deployment it
upgrades older cached records in batches of 500 without deleting existing
analysis or blocking the UI. Missing measurements remain explicitly
missing; `audio_sync_pending` and the coverage counters distinguish records
still awaiting synchronization from never-analyzed tracks. Malformed or
non-finite values are preserved with explicit markers. The exporter cannot
produce analysis that upstream AudioMuse has never computed.

The ZIP may contain metadata from your personal collection, including
upstream-provided source paths in the *raw AudioMuse* records. Review it
before sharing beyond your private assistant conversation.

### Returning a proposed playlist

Upload the ZIP to the assistant and ask it to return **proposal.json**
using the exact track rating keys. Import that file using
**Import playlist proposal**. You may also put `proposal.json` at the
top level of a ZIP and upload that. The schema is:

```json
{
  "format": "plex-playlist-proposal-v1",
  "source": {
    "library_revision": "COPY manifest.json library_revision",
    "playlist_revision": "COPY manifest.json playlist_revision"
  },
  "playlists": [
    {
      "name": "Hard Bop Selection",
      "action": "create",
      "rating_keys": ["123", "456", "789"],
      "description": "Original proposal and editorial intent",
      "notes": "Reasoning to display during review"
    },
    {
      "name": "Existing Playlist",
      "action": "update",
      "rating_keys": ["123", "789", "456"],
      "notes": "A smoother track order"
    }
  ]
}
```

Use actual hexadecimal revision strings from `manifest.json`; the
example identifiers are placeholders. `rating_keys` are strings and
give the **complete target ordered track list**, not just additions.
Only `create` and `update` are supported. A new playlist may
contain 1–120 tracks, an updated playlist 1–1000, and each package up
to 30 playlists. Track IDs must be unique and must exist in the current
Plex catalogue. Existing descriptions and artwork are preserved;
`description` is supported for new playlists only.

The preview reads the **current live Plex track order**. It refuses
stale catalogue/playlist revisions, unknown Plex IDs, case-insensitive
name collisions, hidden smart/radio playlists, and updates that do not
match a saved YAML definition and an existing live Plex playlist.
Review each exact target track list, explicitly tick the approval
checkbox, then choose **Approve and apply proposed changes**.

- Existing playlists use the same in-place curation transaction as
  Playlist Intelligence: their Plex playlist IDs and covers survive;
  YAML is updated after Plex verifies the order, with rollback attempts
  on failure.
- New playlists go through the existing durable draft and
  generation-publication transaction, which checks IDs, creates the
  playlist and saves the corresponding YAML.
- Unmentioned playlists are never modified. Preview does not write
  to Plex, YAML or the draft database.
- A multi-playlist approval is **sequential, not all-or-nothing**.
  If one operation fails, later playlists are not attempted and the
  result names every successfully applied, failed and untouched item.
  Re-export and review again before retrying a partial application.
  Approval tokens become invalid if Plex, saved YAML, the submitted
  proposal or the application process changes.

API: `GET /api/collaboration/export.zip`,
`POST /api/collaboration/proposals/preview` (UTF-8 JSON or ZIP body),
`POST /api/collaboration/proposals/apply` (same-origin JSON with
`proposal`, `review_token`, `approved: true`).

## Genre-aware playlist creation (AudioMuse)

Open **Playlists & library → Create** at `http://media-server:5051/playlists?work=create` to create a
new playlist from the existing Plex Music catalogue. The creation screen now uses structured **Genre**, **Subgenre**, **Track count**, **Variation** and **Sequence** choices. The musical description is optional and kept under advanced settings. Changing genres does not leave an unrelated pre-filled text brief; structured controls alone are sufficient to create a draft. Optional reference recordings are shown when the selected style lacks evidence. The installed preset is:

- **Genre:** Jazz
- **Subgenre:** Hard bop
- **Track count:** exactly 30, no duplicates
- **Variation:** Balanced
- **Flow:** Smooth transitions

The generator is an extension of the original `playlist_generation.py` engine.
It does not create a second draft store or publishing pipeline.

**Data preparation:** The AudioMuse analysis task must finish. Playlist Manager automatically synchronizes completed results into its local cache. The generator displays matched
Plex IDs, valid MusiCNN/CLAP embeddings, pending records, and other coverage.
The readiness status is based on the **last synchronized** manifest, not proof
that an upstream analysis task is current. The background worker downloads up
to 500 changed tracks per batch and continues while records are pending.
The page loads current cached coverage automatically, without a manual sync step.

**Selecting a subgenre:** The program identifies verified examples from
explicit Plex subgenre tags and optionally from user-selected positive
examples. Search results appear as you type; choose tracks or full albums
from an album; negative examples help distinguish related Jazz styles. The
user needs at least two analyzed, labeled or confirmed positive examples to
establish a defensible genre reference profile. General `Jazz` metadata is
not considered proof of `Hard bop`. Without reference evidence, generation
stops with an explanatory error.

The engine compares all eligible Plex tracks using bounded prototypes derived
from MusiCNN and CLAP embeddings. Missing genre metadata does not automatically
exclude a recording whose audio matches the verified reference set. Rejection
of unsuitable musical evidence is recorded separately from the raw embeddings.
The displayed numbers are **relative acoustic similarities**, not calibrated
genre probabilities.

**Review:** Generation creates an unpublished, versioned draft. Inspect
track-level genre evidence, playback, adjacent-track transitions, artist and
album diversity, and coverage. Add, replace, remove or reorder recordings.
Compare the original and proposed audio before accepting or rejecting a
candidate. Rejections persist within the draft and are withheld from automatic
suggestions. If Plex or AudioMuse changes, use **Regenerate with current
analysis** to explicitly replace the draft's proposed track set; no automatic
regeneration runs in the background.

**Publish:** The existing publication screen requires a final review and
checkbox approval, confirms exactly 30 tracks for this structured preset, and
creates a new Plex playlist plus matching YAML. Existing playlists are not
overwritten. The service protects draft and source revisions and reports
rollback/reconciliation failures rather than pretending a partial operation
succeeded.

**Live media-server acceptance** (after deploying the updated container):

```bash
cd ~/media-stack/plex-playlist-manager
bash deploy/verify_genre_generator.sh
# To save an unpublished 30-track acceptance draft from real Plex/AudioMuse:
GENRE_POSITIVE_REFS="123,456" bash deploy/verify_genre_generator.sh --draft
```

Replace `123,456` with two verified, analyzed Hard bop track rating keys
when the Plex library lacks explicit Hard bop genre tags. The default probe
is read-only. `--draft` creates an unpublished review draft only; it never
creates or changes a Plex playlist or playlist YAML. Paste the printed draft ID
into **Create → Open existing draft** and review it. Final publication remains
an explicit browser approval. Do not treat successful mock CI as this
live-system gate.

**Supported structured API:** `POST /api/generation/drafts` accepts
`genre`, `subgenre`, positive and negative reference Plex IDs, exclusions,
year limits, and `evidence_threshold` in addition to the legacy prompt and
identity. Supporting routes provide readiness, genre-profile availability,
Plex reference search, versioned listening reviews and explicit regeneration.
Existing legacy prompt requests remain supported.

Prior listening judgments for genre fit are reused when generating later drafts
of the same genre/subgenre. Rejections for contextual energy, mood or transition
fit remain local. This feedback lives in the existing generation-draft database.

An existing draft may be reopened by ID in **Create → Open existing draft**,
including drafts generated by the optional live acceptance probe below.

**Verification:** CI includes synthetic Hard bop and neighboring Jazz fixtures,
complete-catalogue retrieval beyond the former 500-track shortlist, strict
30-track acceptance, AudioMuse synchronization, draft review, source revisions,
publication and rollback, plus interactive browser tests. A successful CI run
does not establish that your actual Plex/AudioMuse deployment meets the
live-server acceptance gate.

## Unified downloads and Plex playlist workflow

**Playlist Manager is the single browser entry point** on port **5051**.
The yt-dlp downloader stays a separate, unprivileged media-processing
application (with its internal root-owned WireGuard controller) on Docker's
private network, rather than combining its processes and VPN permissions
with playlist management.

Open `http://media-server:5051/downloads` (substitute your LAN hostname).
Submitting a download, uploading/removing cookies and following job links
return to this canonical page. The old `/downloads/engine/` browser homepage
redirects here; the engine's API paths stay internal.
The Downloads page renders the **complete yt-dlp interface directly in the
Music workspace page**, with the shared navigation, library actions and one
continuous scroll surface. There is no iframe or nested downloader dashboard.
The downloader's own HTML and scripts remain authoritative, while its API,
forms, cookies, static assets and Chrome extension use the fixed same-origin
proxy at `/downloads/engine/`. If the downloader is unavailable or its page
structure changes unexpectedly, the workspace shows a navigable error screen
instead of a partial interface. All existing controls remain available:

- Audio-only URL submission, album/playlist detection, queue progress, retry
  all failed, per-job retries, discard, full download logs and historical jobs.
- Missing Spotify/YouTube track resolution, metadata approval and safe
  publishing of complete albums into `/srv/completed/music`.
- YouTube `cookies.txt` upload, remove and online verification; the standby
  Surfshark WireGuard profile manager and explicit VPN disconnect.
- Browser Chrome-extension ZIP and its original download API. Set the
  extension's server address to
  `http://media-server:5051/downloads/engine`.

Playlist Manager also displays a download health summary and lets
you request a Plex Music scan. New music, Download audit and Playlist
Intelligence remain accessible through the single global navigation. A background poller checks published downloader
jobs every 30 seconds and requests a Plex Music scan after *new*
successfully published releases. The first observation establishes a
historical baseline rather than rescanning all old imports. The scan is
requested, not claimed completed; Plex still indexes asynchronously.
Successful scan requests are recorded idempotently in the mounted file
`/data/downloader-bridge.json`, with a validated `.bak` and corrupt-state
quarantine. If Plex is unavailable, the pending scan is persisted and retried.

The old `~/media-stack/yt-dlp-server` checkout is no longer read by
Docker after migration. It is deliberately retained as a rollback source;
remove or archive it only after confirming the new deployment works.
No data or container volume is deleted by the migration.

**Downloaded music is never automatically inserted into a playlist.**
Review the new recordings in Plex and add them through Playlist Intelligence
with the existing explicit approval and conflict checks. This maintains
all current playlist identities, YAML rules and publication policies.

### One-command migration for your existing media-stack

The deployment uses the two already-present services
`plex-playlist-manager` and `yt-dlp-server`. From the actual host:

```bash
cd ~/media-stack/plex-playlist-manager
git pull --ff-only
bash deploy/apply_media_stack.sh
```

The wrapper pulls **one monorepo checkout** with fast-forward only, refuses
an active/queued download, updates the downloader's build context to
`./plex-playlist-manager/yt-dlp-server`, previews and validates a narrow
update to the parent `docker-compose.yml`, saves an owner-readable private
dated backup, builds both images while current services remain up, and
recreates **only these two services**. The downloader's old external 4545 port is
removed; the two services communicate via Docker DNS
`http://yt-dlp-server:4545`. The existing 5051 binding remains. If the old
standalone `yt-dlp-wireguard-gateway` container is present, the script
removes that obsolete container only.

If `docker-compose.override.yml` is a symlink to the old checkout's
`yt-dlp-server/compose.parent-vpn.yaml`, the script atomically repoints it
to the file in `plex-playlist-manager/yt-dlp-server/`, with rollback on
failed validation. Other overrides are left untouched. The script validates
the **effective merged Compose configuration**, including this override. Unrelated containers and services (Plex, Transmission, Pi-hole,
Finance Dashboard, etc.) and the persisted music, jobs, staging, cookie,
profile, playlist and AudioMuse mounts are left unchanged. The script never
uses `docker compose down`, removes volumes or edits playlist YAML.

Dry-run the Compose rewrite without changing files or containers:

```bash
python3 ~/media-stack/plex-playlist-manager/deploy/merge_media_stack.py \\
  --stack ~/media-stack
```

If a migration validation or build fails, read the error; restore the
`docker-compose.yml.pre-monorepo.<timestamp>.bak` file if needed.
A failed container health check is reported rather than silently claiming
success. After successful deployment, open `/` on port 5051 for the integrated
home, `/playlists` for the playlist workspace or `/downloads` for the
download engine.
Do not attempt migration while download jobs are active or queued.

Security: Playlist Manager proxies only to the configured internal
downloader origin, caps upload sizes and rejects cross-origin browser
mutations. The Chrome extension has narrowly scoped CORS for
`/health`, `/api/probe` and `/download`; extension origins cannot
manage VPN profiles or cookies through that exception. Port 4545 should
remain internal, because the native downloader does not have its own user
authentication.

### Which repository to retire?

Keep **`tpepels/plex-playlist-builder`**. This is the canonical monorepo,
including the `yt-dlp-server/` subtree and both complete Git histories.

After deploying from the monorepo and confirming both containers, the
download queue, Surfshark profiles, cookies and Plex imports, the old
**`tpepels/yt-dlp-server`** GitHub repository can be archived or deleted.
Archiving is safer if you want to retain GitHub issues, releases and
historical workflow runs; a subtree merge preserves Git commits but does not
automatically import that GitHub-specific metadata.

The local `~/media-stack/yt-dlp-server` checkout is no longer part of the
Compose build after migration. Do not remove it during the migration;
retain it for rollback until the new deployment has been verified. Deleting
the GitHub repository or the old local checkout never requires deleting
`/srv/yt-dlp-server`, which contains active persistent data.

### Compose migration safety

The deploy script validates the complete effective Compose configuration,
including your override. It refuses missing music/state/profile mounts,
unexpected build paths, split Docker networks, exposed download-engine
ports, missing VPN privileges, and a missing port 5051 for Playlist Manager.
It verifies the queue is idle before changes and immediately before container
replacement. The downloader must become **healthy** before Playlist Manager
is recreated. Existing Compose YAML is backed up privately; the script does
not recreate unrelated services or remove volumes.

## Intelligent Playlist Generation (new playlists)

Open **/playlist-generator** or choose **Create intelligent playlist** from the
hamburger navigation. The fully connected workflow:

1. Describe the musical goal in plain language, e.g. **Intimate live jazz,
   1955–1965, long improvisations, warm club atmosphere**. Set a name,
   track count (1–120), optional minutes target, and optional explicit style,
   mood, variation, energy, sequence and live-preference settings.
2. The deterministic interpreter records its actual criteria. It understands
   common genres/moods, decade or year interval, long-track requirements,
   track and duration limits and live evidence. **Release years come from Plex
   album metadata**, not validated recording/performance dates. A title cue is
   evidence of a live release, not acoustic proof of club ambience. Missing
   years, style evidence and duration are not fabricated. Users see explicit
   filter exclusions and unsupported musical requests.
3. Re-index the complete Plex music catalogue when necessary. The index
   caches album-derived genre and year together with track durations.
   Select from the **entire** library, exclude speech/applause and proven
   mismatches, balance artist/album variety and musical-neighbor scores, and
   build an ordered sequence for the intended smooth, varied or arc-like flow.
   Candidate counts, metadata basis, AudioMuse coverage and quality warnings
   are shown in the result.
4. Every generated result is saved as a **persistent reviewable draft** in
   \`/data/playlist-generations.sqlite\`. Play, add, replace, remove and reorder
   any recording without changing Plex. Search the catalogue for alternatives.
   The draft can be restored from the generator page after a reload; edits use
   optimistic revisions so an older browser cannot overwrite newer choices.
5. **Review & publish** shows the exact title, ordered tracks, duration,
   five quality dimensions, data coverage and warnings. The user must approve
   explicitly. Publication rechecks draft revision, YAML version, all track
   identities, duplicate names and final Plex ordering. It creates **one new**
   Plex playlist via PlexAPI 4.18.2 and writes the matching ordered YAML
   definition, with conservative rollback if either operation fails. Existing
   playlists are never deleted or replaced. Repeated approval of an already
   published draft is rejected.

AudioMuse continues independently; candidate and final quality scores remain
*heuristics*, not proven listening quality. Missing analysis remains visible,
and a brief with zero proven matches fails explicitly instead of silently
substituting unrelated music. On live jazz briefs, Plex's release year can
differ from the historical recording year; users should review provenance.

Focused, short tests (all run in CI):

\`\`\`bash
python3 -m pytest -q tests/test_playlist_generation.py \
  tests/test_generation_publish.py tests/test_generation_api.py \
  tests/test_curation_library.py
node tests/test_playlist_generator_ui.mjs
\`\`\`

Actual Plex publication requires the user to approve the specific preview
and cannot be marked live-verified until applied to a test playlist on their
server. No background workers, new containers or sudo are needed for this
feature.

## Playlist Intelligence (curation)

Open **/playlist-intelligence** or use the **Playlist Intelligence** link on
the manager's main page.

The full feature workflow is available in one screen:

1. Select a saved playlist. The editor reads the actual ordered Plex playlist;
   it compares its identity and contents against versioned YAML, without writing.
2. Open **Musical identity**. The app proposes a purpose, styles, mood,
   variation (focused/balanced/eclectic), energy target, flow (smooth/arc/varied)
   and live preference based on *explicit* name/description cues plus observed
   Plex genres, AudioMuse mood/energy, artist makeup and energy progression.
   Inference remains marked as such. Edit every field and **Save identity**,
   or reset to the inferred profile. Overrides persist separately in
   `/data/playlist-identities.json`; they do not trigger Plex playlist sync.
3. Select a track to find replacements, or use **Add** to browse the complete
   Plex catalogue. Candidate ranking evaluates existing musical subgroups
   (not only the global centroid), artist/album contribution, fit to the saved
   musical intent and neighboring-track compatibility. It proposes an insertion
   position for additions, with per-factor evidence and coverage-based confidence.
   Without comparable embeddings it shows **unscored**, never invented fit.
   Explicit Plex genres contribute only where available; missing genres are
   never classified by a fabricated model. Audio can be previewed without
   exposing the Plex token in the browser.
4. Stage add, remove, replace or reorder actions. Undo or clear changes.
   **Review changes** is read-only and shows the exact effect before approval.
5. Only **Apply to Plex** with explicit approval edits the existing playlist
   in place. A YAML-version and Plex-order check prevents stale decisions;
   changes are verified, persisted in the saved YAML and marked synchronized.
   The playlist identity and artwork are preserved. Partial failures trigger
   best-effort rollback with an explicit error if reconciliation is required.

The full Plex catalogue is stored atomically in /data/curation-library.json
and refreshed in the background every 15 minutes. Opening curation loads
available playlist names and the selected playlist automatically; the last
selected playlist is restored when possible. A stale catalogue is displayed
immediately while background updates run. Candidate searches update as you
type. Neither the catalogue updater nor AudioMuse changes playlists.

### Step 3 — paired improvement measurement

Every displayed candidate includes a compact read-only comparison; **Review
changes** also compares the **entire proposed ordered playlist**. Five
dimensions are reported independently:

- **Coherence:** best musical-neighbor compatibility from MusiCNN/CLAP features,
  preserving intentional minority subgroups rather than assuming one centroid.
- **Variation:** artist/album Simpson diversity measured against the selected
  focused/balanced/eclectic preference. Diversity is not always maximized.
- **Energy balance:** measured energy relative to the declared target or a
  baseline that is held fixed across before/after comparisons.
- **Flow:** adjacent audio/energy compatibility, varied transitions, or the
  desired rising/falling energy arc, depending on the saved profile.
- **Intent alignment:** available mood/energy/live-release/explicit Plex genre
  evidence and the saved playlist identity.

Each dimension returns before, after, delta, feature coverage, sample size,
and uncertainty. No audio is imputed. An unscored new recording withholds any
improvement claim for dimensions requiring its missing measurements. These
are diagnostic heuristics, **not** listening-quality measurements or
calibrated probabilities. They cannot alone authorize playlist edits.

### Step 4 — real-library, listening-grounded validation

Open **Playlist Intelligence → Audit jazz / ambient / soul / mixed**.
The app now selects **two different real Plex playlists per musical family**
(eight total). The result is marked incomplete if a genre is absent, if its
playlists are missing in Plex, or if AudioMuse coverage is insufficient.
The **Listening validation queue** within each playlist provides representative
track positions plus likely musical outliers and metadata review candidates.
Click a target to compare replacement options from the full Plex catalogue.

**Validate** plays both the proposed and original tracks. The interface
requires at least 15 seconds of each recording's real playback (seeking does
not count), plus an explicit human listening confirmation, before submission.
This verifies interaction in the normal browser workflow but is not a
cryptographically secure proof of human auditory judgment. Reviews accept
or reject a specific pair, with mandatory rejection cause and optional notes.
Feedback is stored in \`/data/curation-validation.sqlite\` and does not change
Plex. Previously rejected alternatives are demoted and marked, not erased.

A portfolio is considered **human validated** only after *every* sampled
playlist reaches at least eight listening reviews from at least four distinct
original tracks and >=60% analyzed audio coverage. The dashboard exposes
acceptance rates and Wilson 95% uncertainty intervals. Model factor weights
are allowed to change only after at least eight reviews (three positive and
three negative), and only if their leave-one-out discrimination is better
than the original weights. Weak or contradictory feedback keeps the baseline.
Even a completed portfolio does **not** prove superiority outside the
sampled music or listeners; use it to identify further concrete errors.

**Real music metadata failure learned from the owner's historical export:**
recordings titled "Spoken Introduction" (notably Art Pepper's Village
Vanguard album) and interviews/applause should not appear in unsolicited
musical recommendations. They are now excluded from automatic ranking while
remaining discoverable via explicit search. Short instrumental pieces are
flagged for consideration, not silently banned (e.g., Supersilent "14.9",
approximately one minute, in the ambient playlist).

**Offline all-playlist historical check:** run

\`\`\`bash
python3 scripts/audit-playlist-export.py \
  --playlists /path/to/plex-playlists.yml \
  --library /path/to/plex-music-library.tsv \
  --output /path/to/playlist-validation.json
\`\`\`

The validator checks **every** playlist entry for missing IDs, duplicates,
metadata mismatches, speech-like titles, and short candidates for human review.
It never reads or edits audio/Plex and marks its report as metadata-only.
On the owner's 31 October 2026 saved export (111 playlists, 7,001
placements, 17,044 catalogue tracks), metadata matching found zero
unresolved IDs, duplicates, or metadata mismatches. The 167 placements under
two minutes are **review prompts**, not failures. This is a historical,
not an automatically refreshed live-data verification.

The phase cannot be declared musically accepted until the real Plex audio
reviews exist; tests of the app's behavior are not substitutes for listening.

The phase has no automatic playlist modifications. Every change requires a
per-playlist review and a distinct explicit approval.

Short offline checks:

\`\`\`bash
python3 -m pytest -q tests/test_playlist_curator.py tests/test_playlist_intelligence_api.py tests/test_curation_library.py tests/test_playlist_identity_ranking.py tests/test_curation_impact.py tests/test_curation_validation.py tests/test_curation_stages_3_4_api.py tests/test_curation_real_errors.py
node tests/test_playlist_intelligence_ui.mjs
\`\`\`

A live Plex integration check should include preview, comparing a replacement,
approving a change to a test playlist, verifying its ID/cover/contents and
then reverting via the same interface. Live testing changes real Plex data and
must be performed only after the user approves the specific changes.

## Install into your existing media-stack

**For an existing media-stack, use the one-command migration above instead
of manually copying the service snippet.** The following older instructions
are only relevant when setting up Playlist Manager from scratch.

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
      YTDLP_PUBLIC_URL: /downloads
      DOWNLOAD_BRIDGE_SCAN_ENABLED: "1"
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
dashboard visits do not repeatedly scrape the site. The report loads on
opening, refreshes automatically while visible and uses the server's cached
source. A failed fetch uses the previous catalog with a staleness warning,
if one exists. Source failures are reported rather than
inventing releases. A temporary network problem without a cache returns 503.

API: GET /api/discover/isc?refresh=false.

## All-album disk-first reconciliation and failed download history

Open **Missing from Plex** directly from the main navigation or use
**Find albums missing from Plex** on the Downloads page.
The same screen is at `/downloads-audit`; it automatically checks disk
against Plex when opened without requiring a separate Refresh button.
Search for an artist, album or filename, or follow a job's **Check Plex import**
link to `/downloads-audit?tab=history&q=<job-id>`. This page no longer
depends on yt-dlp jobs existing: `GET /api/disk-reconciliation` reads all
actual audio album folders from the downloader's `MUSIC_ROOT` using
`GET /api/disk-inventory` and compares complete disk-relative file paths
to a fresh Plex Music index. It includes the entire music root, not just
YT-DLP Imports, and it pages the album folder results. Files may match even
when the two containers mount music under different root paths, provided
the complete album-relative path suffix agrees. Ambiguous paths are reported
instead of guessed.

Outcomes are **On disk / missing from Plex**, **Partially indexed**,
**Indexed**, and **Ambiguous path**. Scans explicitly report whether they
were complete. Plex labels are shown for matches, even when a Beethoven
recording is indexed under a pianist rather than the composer. The separate
staging count identifies files not yet published. If music is confirmed
on disk and the Plex library mounts that folder, expand **Album files
are on disk but missing from Plex?** and use **Request Plex Music scan**.
This sends an explicit Plex scan request and schedules another read-only
comparison; it does not imply the scan has finished or the album was indexed.

The **Download history and errors** tab uses the downloader's permanent
`/api/download-history` endpoint, independent of disk content. Search by
album/title/job ID or by HTTP 403 errors; filters include failed/partial,
incomplete, and published jobs. There is also a history panel within the
yt-dlp web app. Future failures remain visible after manual removal of their
cards, and the server no longer prunes old jobs after 50 records. Old failures
already pruned before this update are not recoverable from the job database.

A failed download need not have any files on disk, so **both** disk inventory
and attempt history are required. A published file missing from Plex still
requires examination of the Plex library's configured roots, folder scanning,
permissions, and file support. Neither scan silently edits audio tags, files,
or metadata.

The full native Downloads page no longer loads the offline fallback
reconnect script; doing so previously caused an unconditional browser reload
on every status probe. Its queue polling now updates cards only when job
data changes, and the retained download history avoids rebuilding itself
on unchanged background polls. The offline fallback alone checks whether
the native downloader has recovered and transitions to it once.

Both containers must be updated for the new endpoints.

## Legacy job-based download audit (API only)

The main navigation's **Missing from Plex** link opens
`/downloads-audit?q=Beethoven` for an artist/album search; the old
job-based API remains available separately.
The read-only `GET /api/download-audit?q=...` compares all retained yt-dlp
jobs with both `GET /api/import-audit` (actual presence of published and
staged files on the downloader's disk) and a fresh Plex Music index
(`music.searchTracks()`, bypassing the normal insights cache).

The comparison uses the complete path relative to `YT-DLP Imports`.
The two services may mount the same music files under different roots. Artist
and album tags are **not** used to claim a match, so a Beethoven recording
indexed under a particular pianist or orchestra is still found and the
actual Plex artist/album labels are shown. Outcomes distinguish:

- **All published files indexed**, with Plex's artist and album labels.
- **Some or no published files indexed** (possibly missing Plex scan, missing
  library folder or storage mount; these cannot be distinguished by labels).
- **Some or all published files now missing on disk**, verified inside the
  downloader container.
- **Waiting for metadata** in yt-dlp staging rather than published to Plex.
- **Still downloading**, **failed**, or **no confirmed publication**.

The page also displays Plex Music library locations when available. Use its
**Plex-bibliotheek scannen** button to explicitly request a new Plex scan
(`POST /api/download-audit/scan`). The response acknowledges the scan
request, **not** completed indexing. Click **Controleren** again after the
scan to re-run the actual file comparison.

Both containers must be updated because the page relies on the downloader's
new `/api/import-audit` endpoint. The audit only covers jobs still present
in yt-dlp's retained history; jobs deleted or pruned before the update are
not reconstructed. It never modifies files or metadata automatically.

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
or directory, or download the complete report. The report updates when
opened and periodically; no rescan button is required.

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

The single playlist workspace includes **My playlists**, **Create**, **Improve**,
**Discover**, **Reports** and **Audio analysis** through the global navigation.
Health, duplicates and new-music reports load when opened and refresh
automatically while visible. They compare saved playlists with cached Plex
music and never require a manual reload.

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
`Vol.` / `Volume`, and `Pt.` / `Part` are equivalent. A leading artist
credit repeated in the supplied album title is also redundant when it matches
the **full artist name exactly**: `Jim Hall — Jim Hall Live!` matches the
catalog album `Jim Hall — Live!`. The search also retries without that
repeated title prefix. This rule never removes arbitrary words. Only a fixed list of
trailing release qualifiers (e.g. Deluxe, Remastered, Complete, Expanded) can
be removed, and a requested **Complete** release will not resolve to a shorter
edition. `(Live)`, `(Demo)`, `(Acoustic)`, `(Mono)`, `(Stereo)`, and volume
numbers remain meaningful. Two exceptions are explicitly documented in the
resolver: Stan Getz's *Getz at the Gate (Live)* and Miles Davis's *Blackhawk
Complete (Live)* catalog label. New exceptions require intentional code changes.

All bounded catalog searches are evaluated before choosing a result. Exact
titles rank first, followed by a redundant exact artist prefix, recognized
editions and explicit aliases. Two different
album IDs tied at the best rank yield `status: ambiguous`: neither is sent
to the downloader. The resolved response includes the applied
`normalization_rules`, search queries and candidate count, which are passed
to the downloader for persistent provenance.

API routes: `GET /api/youtube-album?artist=...&album=...` for read-only
resolution, `GET /open-youtube-album?artist=...&album=...` for navigation,
and `POST /queue-youtube-album` for the signed download action. The album
link now returns an **immediate lightweight HTML page**, rather than waiting
for YouTube Music to resolve before the browser displays anything. Client-side
JavaScript shows an activity indicator, real elapsed time, lookup status and
the subsequent Plex/yt-dlp submission status. An accepted request displays
the returned job ID, queue status and whether this album was already queued,
then opens the downloader dashboard. Errors remain visible with a retry
action; ambiguous matches show their candidate URLs without downloading.

The signed POST supports `Accept: application/json` and returns
`{ok, job_id, latest_job_id, status, duplicate, duplicate_reason,
duplicate_requests, retried, visible, active, message, downloader_url}`. The manager submits a verified album to yt-dlp,
then makes a second request to check that the job genuinely exists and is
visible before redirecting. It never treats an old, failed, or pruned
idempotency claim as an active queued job. A previously failed/partial
download is retried through yt-dlp's native Retry / continue endpoint and
the new attempt is verified before redirecting. The downloader URL includes
`?job=<latest_job_id>` to highlight that specific job. If yt-dlp confirms
an already completed job that is no longer visible, the manager remains on
its status page with an explanation and an optional dashboard link.
Traditional form POST requests retain the existing HTTP 303 redirect.
On an existing active job the progress page now reports **Duplicate download
prevented**, explains that the same YouTube album ID already has a job, and
offers a link to inspect it **without automatically leaving the explanation**.
A newly queued job (or a native retry) opens the corresponding highlighted
card after acknowledgement. An exact album already owned in Plex gets
**Album already in Plex** with no retry prompt, and a completed historical
job is explicitly distinguished from an active download. yt-dlp archive
skips are separately reported on the downloader's job card.

When a single catalog match is **exact** (including a redundant leading
artist prefix) and has an exact artist credit, the browser automatically
submits the signed POST after resolving it. There is **no second click**.
For edition variants, explicit album aliases and ensemble artist credits,
the page requires review and a button click. A plain GET does not enqueue
anything; non-JavaScript clients can still use the manual button.
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

**Note:** Ordinary link-preview fetches do not queue downloads, because
GET is read-only; a browser executing the page JavaScript will auto-submit
strict exact matches. Links that are rendered by automated browsers capable
of running JavaScript may also trigger downloads. The downloader's persistent
album-ID deduplication prevents repeat jobs for the same YouTube album ID.
Equally ranked catalog matches are deliberately never downloaded automatically.

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


## Reliability and recovery (November 2026)

### Playlist synchronization and import approvals

All playlist sync endpoints, including legacy single-playlist, bulk and
YAML-import flows, use identity-preserving in-place Plex updates for existing
playlists. A failed ordering mutation attempts to restore the exact previous
order, and unresolved rollback failures require explicit manual reconciliation.
New playlists are created only when the requested name does not already exist.
Smart/radio playlists, ambiguous case-insensitive names and duplicate Plex
track IDs are blocked. Existing Plex playlist IDs and attached covers survive
normal synchronization.

Selecting a YAML file now **only previews** its exact new/updated definitions.
The importer displays before/after item lists and requires a checked approval
box and an explicit Apply action. The review token binds the file to the current
saved YAML revision for 15 minutes; stale versions and unapproved writes fail
closed. On approval, only the new/updated playlist names from **this file** are
eligible for import-triggered Plex sync. Earlier pending changes are retained
for a separate review. The global Sync pending action remains a deliberate
separate operation.

### Immediate AudioMuse analysis after verified downloads

When the downloader publishes music and requests a Plex scan, the scan bridge
**wakes the existing album verifier**. It checks Plex's actual indexed file
paths, rather than trusting the download job status or a successful scan
request. While an import is unresolved, the verifier rechecks every 45 seconds;
during idle periods it still checks every three minutes.

On the first transition of a newly downloaded album to **fully indexed**, the
verifier converts the exact Plex track rating keys and parent album keys to a
durable admission into the **existing** automatic AudioMuse queue. It starts the
normal AudioMuse controller immediately when the upstream is idle, and records
the normal upstream task ID. If AudioMuse is already busy, the same queue waits
for the existing two-minute worker. Unmatched/ambiguous file identities are
never guessed from album names and remain retryable. Failed queue writes remain
pending across restarts; repeated verification uses the same job ID so the
album cannot be enqueued twice.

The regular 15-minute Plex catalogue observer and two-minute AudioMuse queue
worker remain in place for music added outside the downloader, missed events
or downtime. An initial scan of the existing downloader archive is treated as
historical, not an instruction to reanalyze the whole collection. If a new
download coincides with first-start catalogue initialization, missing per-track
embeddings still cause the album to be queued.

Inspect `GET /api/album-verification` for `analysis_handoff` per download
(`pending`, `queued`, or `historical`). The Downloads diagnostics page shows
the same state. Inspect `GET /api/workspace/maintenance` for the actual
AudioMuse queue, active task, and coverage verification. A queued task is not
yet proof of completed analysis. Upstream AudioMuse currently accepts only
`num_recent_albums`, so the verification/retry layer remains necessary.

### Real album and AudioMuse completion evidence

A background album verifier starts shortly after boot and checks every
retained downloader job (including old jobs) against actual published disk
paths and Plex's current indexed file paths. It persists per-album stages in
`/data/album-verification.json`: downloading, metadata required, not
published, missing on disk, awaiting index, scan requested/failed, indexed,
partially downloaded, unverifiable or requires attention. It batches scan
requests (one Plex scan per pass), observes the shared scan timestamp, waits
10 minutes between attempts and stops after three automatic attempts.
Successful publication or accepted Plex scan does **not** mean indexing
completed. A partial source download is never labeled a complete album.
The `/api/album-verification` endpoint is read-only; history cards link to
the underlying album.

Automatic AudioMuse tasks similarly require evidence that every intended
Plex track has both valid MusiCNN and CLAP embeddings. The cache is
synchronized after a task completes, missing album targets are retried up to
three attempts, and exhausted targets are explicitly marked for manual review.
The AudioMuse control API supports only the most recent *N* albums, not
exact album IDs. Therefore retries may not include older targets; the
application **reports this limitation** and never falsely certifies coverage.
Use `GET /api/workspace/maintenance` to inspect actual coverage evidence.

Saved playlist fingerprints are compared independently against live Plex
identity, membership and ordering every five minutes. Missing/ambiguous,
unverifiable and changed playlists remain visible without making any
automatic Plex modifications. Review `GET /api/playlist-drift` and the
playlist workspace's live verification status.

### Diagnostics, performance and measured quality

The Home page is the single operational view: queued/failed downloads,
unindexed files, live playlist drift, AudioMuse coverage, cache health and
background failures link to the relevant correction screens. Disk-to-Plex
comparison reuses a complete 120-second snapshot when users change
filters; Plex scan requests explicitly invalidate it.

Playlist generator and collaboration approval screens now report artist and
album concentration, consecutive repetitions, measured AudioMuse similarity,
energy jumps, missing embedding coverage and exact-ID overlap with existing
collections. These are relative metrics with explicit denominators, **not**
validated probabilities of musical quality or invented missing measurements.

### Deploy with a verified recovery snapshot

Use the canonical deployment script from the existing checkout:

```bash
cd ~/media-stack/plex-playlist-manager
bash deploy/apply_media_stack.sh
```

It refuses active downloads, fast-forwards `main`, and **re-executes the
newly pulled deployment script** if Git changed it (preventing stale Bash
logic from executing the newer backup code). It verifies frontend
contracts and creates a **checksum-verified private state backup** before
modifying Compose, builds both services, waits for downloader health and
smoke-tests the Home, playlist, genre-generation, Downloads and diagnostics
pages and the status APIs. By default the verified backups are retained
under `~/media-stack/private-state-backups/` (up to seven versions). This
uses the existing playlist-manager Docker image as an **isolated backup
reader**. The container has no network, both state volumes are mounted
read-only, and its only writable bind mount is the private backup directory.
This resolves host-user permission failures on root-owned state files (for
example, `album-verification.json`) without `chmod` or `chown` on the
live volume. A host-side invocation of `backup_music_workspace.py` also
falls back to the isolated reader after a `PermissionError`; the reader
passes explicit owner IDs to prevent recursive fallback. The backup archives are mode `0600` and owned by the deploy
user; the directory is mode `0700`. Missing Docker images, inaccessible
state or an incomplete archive stop deployment before any rebuild.
Archives include sensitive downloader state and must stay on private,
encrypted storage.

Restoration requires stopping both containers first. The following is
**read-only** and verifies every file checksum while showing exact paths:

```bash
python3 deploy/backup_music_workspace.py \
  --manager-data /srv/plex-playlist-manager \
  --downloader-state /srv/yt-dlp-server/state \
  --restore /path/to/music-workspace-YYYYMMDDTHHMMSSZ.zip
```

Only add `--apply` after checking the plan and stopping services.
For a restore that writes root-owned state files, use the command under
`sudo` after reviewing the read-only plan; the deployment script itself
must **not** be run under `sudo`. No blanket ownership or permission
changes to `/srv` are needed. The restore script rejects running
`plex-playlist-manager` or `yt-dlp-server` containers.
Existing files receive a `.pre-restore` backup; the tool will not silently
overwrite an existing rollback copy. SQLite files are exported via the
SQLite backup API, including when the application is running.
Never serve these archives from an HTTP download endpoint.

The web application only accepts direct socket peers from RFC1918, loopback,
Tailscale/CGNAT, local IPv6 or explicitly configured networks by default.
Set `WORKSPACE_ALLOWED_CIDRS` to adjust trusted subnets. Setting it to
`*` explicitly disables this safety control. A public reverse proxy must
still enforce its own authentication; the app does not authenticate users
within a trusted LAN.

CI includes regression checks for the destructive-sync replacement,
import-review signatures, live Plex drift, real album file matching and scan
retries, AudioMuse coverage, UI polling, composition evidence and verified
backups. Live Plex/AudioMuse acceptance is checked further by the deployment
smoke tests, but physical playback and a real imported album still require
on-server verification. GitHub branch protection must be enabled by a
repository administrator because the active GitHub connector does not
provide branch-protection mutations.
