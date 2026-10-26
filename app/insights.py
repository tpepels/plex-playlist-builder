"""Read-only playlist analytics and recently added album coverage.

Calculations use Plex library track keys and saved playlist definitions.
They never modify music files or playlist definitions.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import re
import unicodedata


TARGETS = {"max_artist_share": 0.20, "max_album_share": 0.25, "min_recent_share": 0.15}
MIN_SIZE_FOR_CAPS = 12


def normalize(value):
    value = unicodedata.normalize("NFKD", str(value or "")).casefold()
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def timestamp(value):
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def track_model(track):
    artist = str(getattr(track, "grandparentTitle", "") or "")
    album = str(getattr(track, "parentTitle", "") or "")
    title = str(getattr(track, "title", "") or "")
    key = str(getattr(track, "ratingKey", "") or "")
    album_key = str(getattr(track, "parentRatingKey", "") or "") or (
        normalize(artist) + "|" + normalize(album)
    )
    return {
        "key": key, "album_key": album_key, "artist": artist, "album": album,
        "title": title, "added": timestamp(getattr(track, "addedAt", None)),
    }


def analyze(tracks, definitions, policies=None, now=None, recent_days=30, latest_limit=100):
    """Produce dashboard summary, per-playlist metrics and recent album review.

    Rating keys are authoritative; fallback artist/title matching requires
    an unambiguous exact normalized match, or the entry is marked unresolved.
    """
    if not 1 <= recent_days <= 365 or not 1 <= latest_limit <= 500:
        raise ValueError("Invalid recent window or album limit.")
    now = timestamp(now) or datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(days=recent_days)
    policies = policies or {}

    library = {}
    album_index = defaultdict(list)
    by_album = defaultdict(list)
    by_track = defaultdict(list)
    for raw in tracks:
        t = track_model(raw)
        if not t["key"]:
            continue
        library[t["key"]] = t
        by_album[(normalize(t["artist"]), normalize(t["album"]))].append(t["key"])
        by_track[(normalize(t["artist"]), normalize(t["title"]))].append(t["key"])
        album_index[t["album_key"]].append(t)

    # Keep input order for playlist listings, deduplicate each matched item.
    def resolve(item):
        key = str(item.get("rating_key") or "").strip()
        if key:
            return ([key], None) if key in library else ([], "Missing Plex rating key " + key)
        artist = normalize(item.get("artist"))
        album = normalize(item.get("album"))
        song = normalize(item.get("track"))
        if not artist or not (album or song):
            return [], "Incomplete playlist item"
        if song:
            choices = by_track.get((artist, song), [])
            if album:
                choices = [key for key in choices if normalize(library[key]["album"]) == album]
            if len(choices) == 1:
                return choices, None
            return [], "Track ambiguous" if choices else "Track not in library"
        choices = by_album.get((artist, album), [])
        return (list(dict.fromkeys(choices)), None) if choices else ([], "Album not in library")

    playlist_metrics = []
    coverage = defaultdict(set)
    memberships = defaultdict(set)
    all_occurrences = Counter()
    missing_refs = 0
    for name, spec in definitions.items():
        counts = Counter()
        errors = []
        for item in spec.get("items", []):
            keys, error = resolve(item)
            if error:
                missing_refs += 1
                errors.append(error)
            for key in keys:
                counts[key] += 1

        included = list(counts)
        artists = Counter(library[key]["artist"] for key in included)
        albums = Counter(library[key]["album_key"] for key in included)
        recent = sum(bool(library[key]["added"] and recent_cutoff <= library[key]["added"] <= now) for key in included)
        n = len(included)
        repeats = sum(count - 1 for count in counts.values())
        kind = policies.get(name, "curated")
        top_artist, top_artist_count = artists.most_common(1)[0] if artists else ("", 0)
        top_album_count = albums.most_common(1)[0][1] if albums else 0
        artist_share = round(top_artist_count / n, 3) if n else 0
        album_share = round(top_album_count / n, 3) if n else 0
        fresh_share = round(recent / n, 3) if n else 0
        flags = []
        if errors:
            flags.append("Missing library matches")
        if repeats:
            flags.append("Repeated tracks")
        # Only rotating playlists receive automatic diversity warnings. Curated
        # playlists are deliberately left alone, even for single-artist themes.
        if kind == "rotating" and n >= MIN_SIZE_FOR_CAPS:
            if artist_share > TARGETS["max_artist_share"]:
                flags.append("Artist concentration")
            if album_share > TARGETS["max_album_share"]:
                flags.append("Album concentration")
            if fresh_share < TARGETS["min_recent_share"]:
                flags.append("Needs discoveries")
        for key in included:
            coverage[key].add(name)
            memberships[library[key]["album_key"]].add(name)
            all_occurrences[key] += 1
        playlist_metrics.append({
            "name": name, "kind": kind, "tracks": n,
            "artists": len(artists), "albums": len(albums),
            "artist_diversity": round(len(artists) / n, 3) if n else 0,
            "album_diversity": round(len(albums) / n, 3) if n else 0,
            "top_artist": top_artist, "top_artist_share": artist_share,
            "top_album_share": album_share, "recent_tracks": recent,
            "recent_share": fresh_share, "repeated_tracks": repeats,
            "missing_references": len(errors), "missing_examples": errors[:4],
            "flags": flags,
        })

    recent_albums = []
    for album_key, entries in album_index.items():
        dated = [entry["added"] for entry in entries if entry["added"]]
        if not dated:
            continue
        latest_date = max(dated)
        if not recent_cutoff <= latest_date <= now:
            continue
        found = sum(bool(coverage.get(entry["key"])) for entry in entries)
        names = sorted(memberships.get(album_key, []))
        examples = [m["name"] for m in playlist_metrics if
                    m["name"] in names][:5]
        album_tracks = len(entries)
        recent_albums.append({
            "artist": entries[0]["artist"], "album": entries[0]["album"],
            "album_key": album_key, "added_at": latest_date.isoformat(),
            "tracks": album_tracks, "covered_tracks": found,
            "coverage_share": round(found / album_tracks, 3),
            "playlists": examples, "playlist_count": len(names),
            "needs_attention": found < album_tracks / 2,
        })
    recent_albums.sort(key=lambda a: (a["added_at"], a["artist"], a["album"]), reverse=True)

    covered = sum(key in coverage for key in library)
    return {
        "summary": {
            "library_tracks": len(library),
            "listed_tracks": covered,
            "unlisted_tracks": len(library) - covered,
            "coverage_share": round(covered / len(library), 3) if library else 0,
            "repeat_placements": sum(max(0, n - 1) for n in all_occurrences.values()),
            "in_multiple_playlists": sum(n > 1 for n in all_occurrences.values()),
            "missing_references": missing_refs,
            "playlists": len(playlist_metrics),
            "rotating_playlists": sum(p["kind"] == "rotating" for p in playlist_metrics),
            "recent_albums": len(recent_albums),
            "uncurated_recent_albums": sum(a["needs_attention"] for a in recent_albums),
            "recent_days": recent_days,
            "targets": TARGETS,
        },
        "playlists": playlist_metrics,
        "recent_albums": recent_albums[:latest_limit],
    }
