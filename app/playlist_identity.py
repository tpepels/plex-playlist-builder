"""Persistent, inspectable musical intent per playlist.

A playlist's identity is informed by its name and description, observed Plex
membership, and AudioMuse. Semantic inferences use *explicit* keyword cues;
they are not represented as machine-measured genres or verified intentions.
Manual overrides are stored separately from synchronized playlist YAML.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import threading

from .insights import normalize

IDENTITIES_FILE = Path(os.getenv("CURATION_IDENTITIES_FILE", "/data/playlist-identities.json"))
LOCK = threading.RLock()
VARIATIONS = {"focused", "balanced", "eclectic"}
FLOWS = {"smooth", "arc", "varied"}
ENERGIES = {"low", "medium", "high", "adaptive"}
LIVE = {"prefer", "neutral", "avoid"}
MOODS = {"relaxed", "warm", "intimate", "reflective", "melancholic",
         "happy", "energetic", "danceable", "aggressive"}
MAX_STYLES = 12
FIELDS = {"purpose", "styles", "moods", "variation", "energy", "flow", "live"}

STYLE_CUES = {
    "jazz":("jazz","bebop","bop","swing","blue note"),
    "ambient":("ambient","drone","stillness","atmospheric"),
    "classical":("classical","orchestral","sonata","chamber","symphony"),
    "soul":("soul","neo soul","motown"),
    "funk":("funk",),
    "blues":("blues",),
    "folk":("folk","acoustic","singer songwriter"),
    "electronic":("electronic","electronica","synth","downtempo"),
    "hip hop":("hip hop","trip hop","beats"),
    "piano":("piano","keys","keyboard"),
    "strings":("strings","cello","violin"),
    "rock":("rock",),
    "latin":("latin","bossa","samba"),
    "vocal":("vocal","voice","voices","singers"),
}
MOOD_CUES = {
    "relaxed":("relaxed","quiet","calm","slow","chill","soft","rustig","zacht","still"),
    "warm":("warm","velvet","cozy","cosy","sunlight"),
    "intimate":("intimate","close","club","small room"),
    "reflective":("reflective","thoughtful","meditative","contemplative"),
    "melancholic":("melancholic","sad","late night","blue hour","moody"),
    "happy":("happy","bright","uplifting","cheerful"),
    "energetic":("energetic","driving","upbeat","intense"),
    "danceable":("dance","groove","dancing","party"),
    "aggressive":("aggressive","heavy","hard hitting"),
}


class IdentityConflict(ValueError):
    pass


def _text(value, maximum):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError("Invalid identity text.")
    return value.strip()


def _tags(raw, allowed=None):
    if not isinstance(raw, list) or len(raw) > MAX_STYLES:
        raise ValueError("Expected at most 12 identity tags.")
    tags = []
    for value in raw:
        value = _text(value, 48).casefold()
        if not value or (allowed and value not in allowed):
            raise ValueError("Invalid identity tag.")
        if value not in tags:
            tags.append(value)
    return tags


def validate(raw):
    """Full record only; prevents silent loss of declared identity fields."""
    if not isinstance(raw, dict) or set(raw) != FIELDS:
        raise ValueError("Identity must contain purpose, styles, moods, variation, energy, flow and live.")
    result = {
        "purpose": _text(raw["purpose"], 400),
        "styles": _tags(raw["styles"]),
        "moods": _tags(raw["moods"], MOODS),
    }
    for key, options in (("variation", VARIATIONS), ("energy", ENERGIES),
                         ("flow", FLOWS), ("live", LIVE)):
        if raw[key] not in options:
            raise ValueError("Unknown identity setting: " + key)
        result[key] = raw[key]
    return result


def _read():
    try:
        data = json.loads(IDENTITIES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise IdentityConflict("Playlist identity storage unreadable.") from exc
    if not isinstance(data, dict):
        raise IdentityConflict("Invalid playlist identity storage.")
    return data


def get_override(name: str):
    with LOCK:
        data = _read()
        value = data.get(name)
        if value is None:
            return None
        try:
            return validate(value)
        except (ValueError, TypeError) as exc:
            raise IdentityConflict("Stored identity is invalid for this playlist.") from exc


def revision(record):
    content = json.dumps(record, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def put_override(name: str, record: dict, expected_revision: str):
    valid = validate(record)
    with LOCK:
        data = _read()
        current = data.get(name)
        if revision(current) != expected_revision:
            raise IdentityConflict("Playlist identity was changed elsewhere. Reload.")
        data[name] = valid
        _persist(data)
    return valid


def delete_override(name: str, expected_revision: str):
    with LOCK:
        data = _read()
        if revision(data.get(name)) != expected_revision:
            raise IdentityConflict("Playlist identity was changed elsewhere. Reload.")
        data.pop(name, None)
        _persist(data)


def _persist(data):
    IDENTITIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = IDENTITIES_FILE.with_name(IDENTITIES_FILE.name + ".tmp")
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, IDENTITIES_FILE)
    finally:
        try: temp.unlink(missing_ok=True)
        except OSError: pass


def _contains(text, cue):
    # Whole phrases, not incidental substrings ("rock" in "brock").
    return re.search(r"(?<![a-z0-9])" + re.escape(cue) + r"(?![a-z0-9])", text) is not None


def infer(name: str, description: str, keys: list[str], library: dict, features: dict):
    """Conservative, deterministic defaults and an evidence trail."""
    source = normalize(name + " " + description)
    styles = [style for style, cues in STYLE_CUES.items()
              if any(_contains(source, cue) for cue in cues)]
    moods = [mood for mood, cues in MOOD_CUES.items()
             if any(_contains(source, cue) for cue in cues)]
    artists = Counter(library[key]["artist"] for key in keys if key in library)
    mix = any(_contains(source, k) for k in ("mix","fusion","eclectic","blend","cross genre","same room"))
    focused = any(_contains(source,k) for k in ("solo","only","unadorned","minimal","minimalist"))
    variation = "eclectic" if mix or len(styles) >= 3 else "focused" if focused else "balanced"
    energy = "low" if any(_contains(source,k) for k in ("calm","quiet","slow","gentle","night","still")) else (
        "high" if any(_contains(source,k) for k in ("energetic","intense","dance","drive","upbeat")) else "adaptive")
    flow = "arc" if any(_contains(source,k) for k in ("journey","crescendo","build","arc")) else (
        "varied" if variation == "eclectic" else "smooth")
    live = "prefer" if any(_contains(source,k) for k in ("live","concert","club","vanguard")) else "neutral"
    default = {
        "purpose": description.strip()[:400] if description.strip() else name[:400],
        "styles": styles[:MAX_STYLES],
        "moods": moods[:MAX_STYLES],
        "variation": variation, "energy": energy, "flow": flow, "live": live,
    }
    tracks = [features[k] for k in keys if k in features]
    measured_energy = [f["energy"] for f in tracks if f.get("energy") is not None]
    mood_counts = {k: sum(k in f.get("mood", {}) for f in tracks) for k in
                   ("relaxed","happy","sad","danceable","aggressive","party")}
    return default, {
        "basis": ["name", "description"] if description.strip() else ["name"],
        "styles_explicit_in_text": styles,
        "analyzed_tracks": len([f for f in tracks if f.get("musicnn") is not None]),
        "energy_tracks": len(measured_energy),
        "mood_track_counts": mood_counts,
        "distinct_artists": len(artists),
        "top_artists": [{"name": k, "count": v} for k,v in artists.most_common(5)],
        "interpretation": "Rule-based name/description cues; verify manually. Audio features describe observed tracks, not stated intent.",
    }


def identity(name, description, keys, library, features, override=None):
    inferred, evidence = infer(name, description, keys, library, features)
    if override is None:
        override = get_override(name)
    return {
        "values": override if override is not None else inferred,
        "suggested": inferred,
        "source": "manual" if override is not None else "inferred",
        "revision": revision(override),
        "evidence": evidence,
    }
