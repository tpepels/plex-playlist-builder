"""Plex playlist identities, profiles and ranked manual curation suggestions.

Data sources: full Plex track catalogue plus AudioMuse's cached MusiCNN/CLAP,
tempo, energy and mood features. Missing analysis is explicitly unknown.
Never mutates playlists.
"""
from __future__ import annotations

import base64
import binascii
from collections import Counter, defaultdict
import hashlib
import json
import math
import struct
from typing import Any

from .insights import normalize, track_model

REVIEW_COVERAGE = .60
MIN_EMBEDDED = 6
MAX_CANDIDATES = 30
MAX_VECTOR_DIMS = 4096


class CurationConflict(ValueError):
    """Client is stale, a playlist cannot safely be edited, or invalid operation."""


def catalogue(raw_tracks: list) -> dict[str, dict]:
    result = {}
    for track in raw_tracks:
        m = track_model(track)
        if not m["key"]:
            continue
        raw_genres = getattr(track, "genres", None) or []
        m["genres"] = list(dict.fromkeys(
            str(getattr(genre, "tag", genre) or "").strip()
            for genre in raw_genres
            if str(getattr(genre, "tag", genre) or "").strip()
        ))[:16]
        result[m["key"]] = m
    return result


def _indexes(library: dict[str, dict]):
    albums, songs = defaultdict(list), defaultdict(list)
    for key, t in library.items():
        albums[(normalize(t["artist"]), normalize(t["album"]))].append(key)
        songs[(normalize(t["artist"]), normalize(t["title"]))].append(key)
    for keys in albums.values():
        keys.sort(key=lambda key: (
            int(library[key]["disc"] or 0), int(library[key]["index"] or 0), key
        ))
    return albums, songs


def resolve_items(spec: dict, library: dict[str, dict]) -> tuple[list[str], list[dict]]:
    """Expand all allowed YAML item forms, preserving order and exact identities."""
    albums, songs = _indexes(library)
    keys, seen, problems = [], set(), []
    for index, item in enumerate(spec.get("items", [])):
        direct = str(item.get("rating_key") or "").strip()
        matches = []
        if direct:
            if direct in library:
                matches = [direct]
            else:
                problems.append({"index": index, "reason": "Missing rating key " + direct})
        else:
            artist = normalize(item.get("artist"))
            album = normalize(item.get("album"))
            title = normalize(item.get("track"))
            if artist and title:
                matches = list(songs.get((artist, title), []))
                if album:
                    matches = [key for key in matches if normalize(library[key]["album"]) == album]
                if len(matches) != 1:
                    problems.append({"index": index, "reason": "Ambiguous track" if matches else "Missing track"})
                    matches = []
            elif artist and album:
                matches = list(albums.get((artist, album), []))
                if not matches:
                    problems.append({"index": index, "reason": "Missing album"})
            else:
                problems.append({"index": index, "reason": "Invalid YAML item"})
        for key in matches:
            if key in seen:
                problems.append({"index": index, "reason": "Duplicate track " + key})
            else:
                keys.append(key)
                seen.add(key)
    return keys, problems


def vector(payload: Any, expected_dim: int | None = None) -> tuple[float, ...] | None:
    if not isinstance(payload, str) or not payload:
        return None
    try:
        data = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error):
        return None
    if len(data) < 16 or len(data) > 4 * MAX_VECTOR_DIMS or len(data) % 4:
        return None
    count = len(data) // 4
    if expected_dim is not None and count != expected_dim:
        return None
    numbers = struct.unpack("<" + "f" * count, data)
    if not all(math.isfinite(x) for x in numbers):
        return None
    size = math.sqrt(sum(x*x for x in numbers))
    if size < 1e-10:
        return None
    return tuple(x / size for x in numbers)


def features_for(cache: dict[str, dict]) -> dict[str, dict]:
    """Normalize vectors once per API request; no guesses for missing features."""
    result = {}
    for key, item in cache.items():
        result[str(key)] = {
            "musicnn": vector(item.get("embedding")),
            "clap": vector(item.get("clap_embedding")),
            "energy": _number(item.get("energy")),
            "tempo": _number(item.get("tempo")),
            "mood": _moods(item),
        }
    return result


def _number(n):
    try:
        result = float(n)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _moods(item):
    raw = item.get("other_features")
    if isinstance(raw, str):
        try: raw = json.loads(raw)
        except ValueError: raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {k: value for k in ("danceable","aggressive","happy","party","relaxed","sad")
            if (value := _number(raw.get(k))) is not None}


def cosine(a, b):
    if a is None or b is None or len(a) != len(b):
        return None
    return max(-1., min(1., sum(x*y for x,y in zip(a,b))))


def _centroid(vectors):
    if not vectors:
        return None
    length = len(vectors[0])
    chosen = [v for v in vectors if len(v) == length]
    if not chosen:
        return None
    total = [sum(v[j] for v in chosen) for j in range(length)]
    norm = math.sqrt(sum(x*x for x in total))
    if norm < 1e-10:
        return None
    return tuple(x/norm for x in total)


def _center(keys, features, field, excluded=None):
    return _centroid([features[key][field] for key in keys
                      if key in features and key != excluded and features[key][field] is not None])


def _mean(values):
    return sum(values)/len(values) if values else None


def _numeric_affinity(x, y, scale):
    if x is None or y is None:
        return None
    return max(0., 1. - abs(x-y)/scale)


def similarity(a: dict, b: dict) -> tuple[float | None, list[str]]:
    """Weighted audio + context. Reweight if optional features are missing."""
    pairs = []
    if (v := cosine(a.get("musicnn"), b.get("musicnn"))) is not None:
        pairs.append((max(0., (v+1)/2), .50, "audio"))
    if (v := cosine(a.get("clap"), b.get("clap"))) is not None:
        pairs.append((max(0., (v+1)/2), .25, "sound"))
    if (v := _numeric_affinity(a.get("energy"), b.get("energy"), 1.)) is not None:
        pairs.append((v, .10, "energy"))
    if a.get("tempo") and b.get("tempo"):
        # Halftime/doubletime can be equivalent; prefer the closest ratio.
        ratios = [abs(math.log2((a["tempo"]*m)/b["tempo"])) for m in (.5,1,2)]
        pairs.append((max(0, 1-min(ratios)), .05, "tempo"))
    common = set(a.get("mood") or {}) & set(b.get("mood") or {})
    if common:
        d = _mean([abs(a["mood"][key] - b["mood"][key]) for key in common])
        pairs.append((max(0, 1-d), .10, "mood"))
    if not pairs or not any(k in ("audio","sound") for _,_,k in pairs):
        return None, []
    score = sum(v*w for v,w,_ in pairs)/sum(w for _,w,_ in pairs)
    return round(score, 4), [k for _,_,k in pairs]


def _profile(keys, lib, ff):
    artists = Counter(lib[k]["artist"] for k in keys)
    albums = Counter(lib[k]["album_key"] for k in keys)
    analyzed = [ff[k] for k in keys if k in ff and ff[k]["musicnn"] is not None]
    energies = [ff[k]["energy"] for k in keys if k in ff and ff[k]["energy"] is not None]
    tempos = [ff[k]["tempo"] for k in keys if k in ff and ff[k]["tempo"] is not None]
    moods = {m: round(_mean([f["mood"][m] for f in analyzed if m in f["mood"]]), 3)
             for m in ("relaxed","happy","sad","danceable","aggressive","party")
             if any(m in f["mood"] for f in analyzed)}
    transitions = []
    for first, second in zip(keys, keys[1:]):
        if first in ff and second in ff and ff[first]["energy"] is not None and ff[second]["energy"] is not None:
            transitions.append(abs(ff[first]["energy"]-ff[second]["energy"]))
    return {
        "tracks": len(keys),
        "artists": len(artists), "albums": len(albums),
        "max_artist_share": round(max(artists.values())/len(keys),3) if keys else 0,
        "max_album_share": round(max(albums.values())/len(keys),3) if keys else 0,
        "top_artists": [{"name":k,"count":v} for k,v in artists.most_common(5)],
        "energy": round(_mean(energies), 3) if energies else None,
        "tempo": round(_mean(tempos), 1) if tempos else None,
        "mood": moods,
        "mean_adjacent_energy_change": round(_mean(transitions), 3) if transitions else None,
        "musicnn_tracks": len(analyzed),
        "coverage": round(len(analyzed)/len(keys),3) if keys else 0,
        "confidence": "sufficient" if len(analyzed)>=MIN_EMBEDDED and
                                    len(analyzed)/max(1,len(keys))>=REVIEW_COVERAGE else "limited",
    }


def assess(name: str, spec: dict, library: dict, cache: dict, intent: dict | None = None) -> dict:
    keys, problems = resolve_items(spec, library)
    ff = features_for(cache)
    profile = _profile(keys, library, ff)
    raw_keys = [str(item.get("rating_key")) for item in spec.get("items", []) if item.get("rating_key")]
    repeats = len(raw_keys) - len(set(raw_keys))
    profile["repeated_tracks"] = repeats
    embedded = [k for k in keys if k in ff and ff[k]["musicnn"] is not None]
    if profile["confidence"] == "sufficient":
        vectors=[ff[k]["musicnn"] for k in embedded]
        common=[v for v in vectors if len(v)==len(vectors[0])]
        count=len(common)
        total=[sum(vec[j] for vec in common) for j in range(len(common[0]))]
        pairwise=(sum(v*v for v in total)-count)/(count*(count-1)) if count>1 else None
        profile["mean_pairwise_cosine"]=round(pairwise,3) if pairwise is not None else None
    else:
        profile["mean_pairwise_cosine"]=None
    reviews = []
    # Contextual outliers: nearest musical neighbors, NOT the global average.
    # A deliberately eclectic playlist can contain coherent subgroups.
    if profile["confidence"] == "sufficient":
        scored = []
        for key in embedded:
            candidate = ff[key]
            neighbors = [cosine(candidate["musicnn"], ff[other]["musicnn"])
                         for other in embedded if other != key]
            neighbors = sorted((v for v in neighbors if v is not None), reverse=True)
            if neighbors:
                scored.append((key, sum(neighbors[:3])/min(3,len(neighbors))))
        baselines = sorted(sc for _,sc in scored)
        median = baselines[len(baselines)//2] if baselines else 0
        variation = (intent or {}).get("variation","balanced")
        threshold = .25 if variation == "eclectic" else .17
        for key, sc in scored:
            if median-sc >= threshold:
                why = ["Weak match to existing musical subgroups"]
                en = ff[key]["energy"]
                if en is not None and profile["energy"] is not None and abs(en-profile["energy"])>.3:
                    why.append("Different energy level")
                tempo = ff[key]["tempo"]
                if tempo and profile["tempo"] and abs(math.log2(tempo/profile["tempo"])) > .7:
                    why.append("Unusual tempo")
                if any(abs(ff[key]["mood"][m]-profile["mood"][m])>.35
                       for m in ff[key]["mood"] if m in profile["mood"]):
                    why.append("Different mood")
                reviews.append({"track_id":key,"reason":why,
                                "difference":round(median-sc,3)})
    reviews.sort(key=lambda it:-it["difference"])
    # Long-term playlist-wide flags: informative, not forced genre caps.
    flags = []
    if profile["max_artist_share"] >= .4 and len(keys)>=12:
        flags.append("Artist concentration")
    if profile["max_album_share"] >= .4 and len(keys)>=12:
        flags.append("Album concentration")
    if profile["mean_adjacent_energy_change"] is not None and profile["mean_adjacent_energy_change"] > .35:
        flags.append("Abrupt energy changes")
    if repeats: flags.append("Repeated tracks: "+str(repeats))
    if problems: flags.append("Unresolved playlist entries")
    tracks = []
    flagged = {i["track_id"]:i for i in reviews}
    for key in keys:
        info=library[key]
        position=keys.index(key)
        transition=None
        if position>0 and key in ff and keys[position-1] in ff:
            last=ff[keys[position-1]]["energy"]
            current=ff[key]["energy"]
            if last is not None and current is not None and abs(current-last)>.35:
                transition="Energy jump after previous track"
        tracks.append({"id":key, "artist":info["artist"], "album":info["album"],
                       "title":info["title"], "analyzed":key in ff and ff[key]["musicnn"] is not None,
                       "energy":ff[key]["energy"] if key in ff else None,
                       "tempo":ff[key]["tempo"] if key in ff else None,
                       "transition":transition,
                       "review":flagged.get(key)})
    return {"name":name, "description":spec.get("description",""),
            "keys":keys, "issues":problems, "profile":profile,
            "flags":flags, "reviews":reviews, "tracks":tracks,
            "sources":{"plex":len(library),"audiomuse":len(cache)}}


def candidates(keys: list[str], library: dict, cache: dict, *,
               target: str | None = None, query: str = "", limit: int = 20) -> list[dict]:
    if limit < 1 or limit > MAX_CANDIDATES:
        raise ValueError("Limit must be between 1 and 30.")
    if target and target not in keys:
        raise CurationConflict("Target track is not in this playlist.")
    ff=features_for(cache)
    center=_center(keys,ff,"musicnn",excluded=target)
    clap=_center(keys,ff,"clap",excluded=target)
    energy=_mean([ff[k]["energy"] for k in keys if k != target and k in ff and ff[k]["energy"] is not None])
    tempo=_mean([ff[k]["tempo"] for k in keys if k != target and k in ff and ff[k]["tempo"] is not None])
    mood={m: _mean([ff[k]["mood"][m] for k in keys if k != target and k in ff and m in ff[k]["mood"]])
          for m in ("relaxed","happy","sad","danceable","aggressive","party")}
    mood={k:v for k,v in mood.items() if v is not None}
    centroid={"musicnn":center,"clap":clap,"energy":energy,"tempo":tempo,"mood":mood}
    normquery=normalize(query)
    pool=[]
    for key, info in library.items():
        if key in keys: continue
        if normquery and normquery not in normalize(" ".join([info["artist"],info["album"],info["title"]])): continue
        score, evidence=similarity(ff.get(key,{}),centroid)
        # When no profile, match target as fallback, never fabricate a score.
        if score is None and target and target in ff:
            score,evidence=similarity(ff.get(key,{}),ff[target])
        pool.append({"id":key,"artist":info["artist"],"album":info["album"],
                     "title":info["title"],"analyzed":key in ff and ff[key]["musicnn"] is not None,
                     "match":score,"evidence":evidence})
    pool.sort(key=lambda item:(item["match"] is None,
              -(item["match"] if item["match"] is not None else -1),
              normalize(item["artist"]),normalize(item["album"]),normalize(item["title"]),item["id"]))
    return pool[:limit]


def signature(items: list[str]) -> str:
    return hashlib.sha256(json.dumps(items, separators=(",",":")).encode()).hexdigest()[:20]


def compute_operations(before: list[str], operations: list[dict], library: dict) -> list[str]:
    if not isinstance(operations,list) or not 1<=len(operations)<=100:
        raise CurationConflict("Select 1–100 changes.")
    result=list(before)
    for op in operations:
        if not isinstance(op,dict):
            raise CurationConflict("Invalid change.")
        kind=op.get("type")
        key=str(op.get("track_id") or "")
        replacement=str(op.get("candidate_id") or "")
        if kind == "add":
            if replacement not in library or replacement in result:
                raise CurationConflict("Candidate missing or already in playlist: "+replacement)
            if "after_id" not in op:
                result.append(replacement)
            elif op["after_id"] is None:
                result.insert(0, replacement)
            else:
                after=str(op["after_id"])
                if after not in result:
                    raise CurationConflict("Insertion anchor missing.")
                result.insert(result.index(after)+1, replacement)
        elif kind in ("remove","replace","move"):
            if key not in result:
                raise CurationConflict("Track is absent: "+key)
            idx=result.index(key)
            if kind=="remove":
                result.pop(idx)
            elif kind=="replace":
                if replacement not in library or replacement in result:
                    raise CurationConflict("Replacement missing or already in playlist: "+replacement)
                result[idx]=replacement
            elif kind=="move":
                after=op.get("after_id")
                if after is not None and str(after) not in result:
                    raise CurationConflict("Move target missing.")
                if after is not None and str(after)==key:
                    raise CurationConflict("Cannot move a track after itself.")
                result.pop(idx)
                result.insert(result.index(str(after))+1 if after is not None else 0,key)
        else:
            raise CurationConflict("Unknown change type.")
    if not result or len(set(result))!=len(result) or len(result)>1000:
        raise CurationConflict("Invalid resulting playlist length or duplicates.")
    if result==before:
        raise CurationConflict("There are no changes to apply.")
    return result


def yaml_items(keys: list[str], library: dict) -> list[dict]:
    return [{"rating_key":key,"artist":library[key]["artist"],
             "album":library[key]["album"],"track":library[key]["title"]} for key in keys]
