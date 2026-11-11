"""Music-prompt-driven generation of ordered playlists from the full Plex catalogue.

All claims are constrained to available Plex metadata and observed AudioMuse
features. The deterministic parser does not silently infer concert recording
dates, instrumentation or atmosphere. No Plex or YAML mutation happens here.
"""
from __future__ import annotations

from collections import Counter
import math
import re
import hashlib
import json

from .insights import normalize
from .playlist_identity import infer, validate, STYLE_CUES
from .playlist_curator import features_for, cosine, _mean, _centroid
from .playlist_ranking import _intent_score, _neighbor_score, clamp, _score_candidate, _profile_context
from .curation_review_rules import automatically_rankable, review_track
from .curation_impact import measure

MAX_TRACKS=120
MIN_TRACKS=1
MAX_MINUTES=900
SELECTION_POLICY_VERSION=2
MIN_GENRE_REFERENCES=2
# Candidate retrieval is complete-catalogue; no arbitrary top-500 truncation.

YEAR_RANGE=re.compile(r"(?<!\d)((?:18|19|20)\d{2})\s*(?:-|–|—|to|through|tot|t\/m)\s*((?:18|19|20)\d{2})(?!\d)",re.I)
YEAR_DECADE=re.compile(r"\b((?:18|19|20)\d0)s\b",re.I)
TRACK_COUNT=re.compile(r"\b(\d{1,3})\s*(?:tracks?|songs?|nummers?|stukken?)\b",re.I)
MINUTES_COUNT=re.compile(r"\b(\d{2,3})\s*(?:minutes?|mins?|minuten?)\b",re.I)
HOUR_COUNT=re.compile(r"\b(\d{1,2})\s*(?:hours?|uur)\b",re.I)
LONG_CUE=re.compile(r"\b(long|extended|lengthy|lang(?:e)?|uitgebreide)\b.{0,25}\b(improvisations?|solos?|tracks?|pieces?|improvisaties?|nummers?)\b",re.I)
LIVE_CUE=re.compile(r"\b(live|concert|club|vanguard|on stage|in concert)\b",re.I)
EXPLICIT_GENRE=re.compile(r"\b(jazz|soul|ambient|blues|funk|classical|rock|folk|electronic)\b",re.I)
PERFORMANCE_CUES=("live","concert","vanguard","at the","in person","jazz club","on stage")
AUDIO_VECTORS=("musicnn","clap")



# Genre identities are distinct from broad style names. Related labels such as
# bebop are intentionally *not* synonyms for hard bop.
GENRE_ALIASES = {
    "jazz": {"jazz"}, "soul": {"soul"}, "ambient": {"ambient"},
    "blues": {"blues"}, "funk": {"funk"}, "rock": {"rock"},
    "classical": {"classical"}, "electronic": {"electronic", "electronica"},
    "folk": {"folk"}, "hip hop": {"hip hop", "hip-hop"},
}
SUBGENRE_ALIASES = {
    "hard bop": {"hard bop", "hard-bop", "hardbop"},
    "bebop": {"bebop", "be bop", "be-bop"},
    "cool jazz": {"cool jazz", "cool-jazz", "west coast jazz"},
    "modal jazz": {"modal jazz", "modal-jazz"},
    "swing": {"swing", "big band"},
    "free jazz": {"free jazz", "avant-garde jazz"},
    "post bop": {"post bop", "post-bop", "postbop"},
}
JAZZ_RELATIVES = {"hard bop", "bebop", "cool jazz", "modal jazz",
                  "swing", "free jazz", "post bop"}


def _style_token(value: str) -> str:
    if not isinstance(value, str):
        raise GenerationError("Genre and subgenre must be text.")
    value = normalize(value.strip().replace("-", " "))
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > 64:
        raise GenerationError("Genre or subgenre exceeds 64 characters.")
    for canonical, names in {**GENRE_ALIASES, **SUBGENRE_ALIASES}.items():
        if value in {normalize(n.replace("-", " ")) for n in names}:
            return canonical
    return value


def _bounded_ids(value, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 150:
        raise GenerationError(field + " must contain at most 150 Plex track IDs.")
    result = []
    for item in value:
        key = str(item)
        if not key.isascii() or not key.isdecimal() or len(key) > 19:
            raise GenerationError(field + " contains an invalid Plex track ID.")
        if key not in result:
            result.append(key)
    return result


def catalogue_revision(library: dict) -> str:
    rows = [(str(k), str(v.get("artist")), str(v.get("album")),
             str(v.get("title")), v.get("year"), v.get("duration_ms"),
             v.get("genres", [])) for k, v in sorted(library.items())]
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _tags(info):
    return {_style_token(t) for t in (info.get("genres") or []) if t}


def _evidence_tags(info, genre, subgenre):
    genres = _tags(info)
    exact = bool(subgenre and subgenre in genres)
    broad = genre in genres if genre else False
    other = bool(subgenre and genres & (JAZZ_RELATIVES - {subgenre}))
    if exact:
        return "verified_subgenre"
    if other:
        return "related_subgenre"
    if broad:
        return "broad_genre"
    if genres:
        return "unrelated_metadata"
    return "missing_metadata"


def _audio_overlap(a, b):
    if not a or not b:
        return None
    return cosine(a, b)


def _audio_affinity(candidate, references):
    """Only comparable measured spaces contribute; values are similarities."""
    result = {}
    for field in ("musicnn", "clap"):
        vector = candidate.get(field)
        values = [(_audio_overlap(vector, item.get(field)) + 1) / 2
                  for item in references
                  if vector is not None and item.get(field) is not None
                  and _audio_overlap(vector, item.get(field)) is not None]
        result[field] = _mean(sorted(values, reverse=True)[:min(5, len(values))])
    return result


def _reference_sample(all_ids, manual_ids, maximum=28):
    """Bound comparisons while retaining explicitly chosen reference tracks."""
    manual = sorted(set(manual_ids))
    automatic = sorted(set(all_ids) - set(manual))
    if len(automatic)>maximum:
        automatic = [automatic[round(i*(len(automatic)-1)/(maximum-1))]
                     for i in range(maximum)]
    return sorted(set(manual) | set(automatic))


def _reference_prototypes(keys, decoded, groups=4):
    """Bounded, deterministic musical prototypes for fast whole-library matching.

    Multiple centroids preserve distinct reference neighborhoods; this avoids
    per-candidate comparisons against thousands of reference tracks.
    """
    keys=sorted(keys)
    if not keys:
        return []
    count=min(groups,len(keys))
    buckets=[keys[i::count] for i in range(count)]
    result=[]
    for bucket in buckets:
        result.append({
            field:_centroid([decoded[k][field] for k in bucket
                             if k in decoded and decoded[k].get(field) is not None])
            for field in ("musicnn","clap")
        })
    return result


def genre_profiles(library: dict, decoded: dict, *, genre: str,
                   subgenre: str = "", reference_track_ids=None,
                   negative_reference_track_ids=None, evidence_threshold="balanced"):
    """Only explicit genre labels and user-confirmed references seed profiles.

    No artist/title guessing and no self-training from the proposed playlist.
    """
    genre = _style_token(genre) if genre else ""
    subgenre = _style_token(subgenre) if subgenre else ""
    positives = _bounded_ids(reference_track_ids, "reference_track_ids")
    negatives = _bounded_ids(negative_reference_track_ids, "negative_reference_track_ids")
    for key in positives + negatives:
        if key not in library:
            raise GenerationError("Reference Plex track not found: " + key)
        if key not in decoded or not any(decoded[key].get(x) is not None
                                         for x in ("musicnn", "clap")):
            raise GenerationError("Reference track lacks a valid AudioMuse embedding: " + key)
    if set(positives) & set(negatives):
        raise GenerationError("A reference track cannot be both positive and negative.")
    positive_set = set(positives)
    negative_set = set(negatives)
    verified = []
    contrast = []
    for key, info in sorted(library.items()):
        measured = decoded.get(key, {})
        if not any(measured.get(x) is not None for x in ("musicnn", "clap")):
            continue
        tags = _tags(info)
        if (subgenre and subgenre in tags) or (not subgenre and genre in tags):
            verified.append(key)
        elif subgenre and tags & (JAZZ_RELATIVES - {subgenre}):
            contrast.append(key)
    positive_set.update(verified)
    positive_set.difference_update(negative_set)
    # A user-confirmed positive always overrides a conflicting metadata label.
    negative_set.update(k for k in contrast if k not in positive_set)
    if subgenre and len(positive_set) < MIN_GENRE_REFERENCES:
        raise GenerationError(
            "Insufficient verified " + subgenre + " references: "
            "select at least two analyzed positive tracks or tag verified examples."
        )
    if genre and not subgenre and len(positive_set) < MIN_GENRE_REFERENCES:
        raise GenerationError(
            "Insufficient analyzed " + genre + " references; select at least two positive tracks."
        )
    positive_sample=_reference_sample(positive_set, positives)
    negative_sample=_reference_sample(negative_set, negatives)
    return {
        "genre": genre, "subgenre": subgenre,
        "positive_ids": positive_sample,
        "negative_ids": negative_sample,
        "positive_prototypes": _reference_prototypes(positive_sample,decoded),
        "negative_prototypes": _reference_prototypes(negative_sample,decoded),
        "verified_metadata_count": len(verified),
        "manual_positive_count": len(positives),
        "manual_negative_count": len(negatives),
        "available": len(positive_set) >= MIN_GENRE_REFERENCES,
        "evidence_threshold": evidence_threshold,
        "method": "explicit_subgenre_or_user_confirmed_audio_references",
    }


def genre_evidence(key, info, decoded, profile, *, reviews=None):
    """Evidence-backed retrieval; acoustic compatibility is NOT genre probability."""
    genre = profile["genre"]
    subgenre = profile["subgenre"]
    support = _evidence_tags(info, genre, subgenre)
    f = decoded.get(key, {})
    pos = profile.get("positive_prototypes") or [
        decoded[k] for k in profile["positive_ids"] if k != key and k in decoded]
    neg = profile.get("negative_prototypes") or [
        decoded[k] for k in profile["negative_ids"] if k != key and k in decoded]
    scores = _audio_affinity(f, pos)
    contrasts = _audio_affinity(f, neg)
    available = [(scores[x], w) for x,w in (("musicnn", .65), ("clap", .35))
                 if scores[x] is not None]
    positive = sum(v*w for v,w in available)/sum(w for _,w in available) if available else None
    compared = [(contrasts[x],w) for x,w in (("musicnn", .65), ("clap", .35))
                if contrasts[x] is not None]
    negative = sum(v*w for v,w in compared)/sum(w for _,w in compared) if compared else None
    margin = positive - negative if positive is not None and negative is not None else None
    labeled = support == "verified_subgenre" or (not subgenre and support == "broad_genre")
    approved_reference = key in profile["positive_ids"]
    if labeled or approved_reference:
        compatible = True
        basis = "verified_metadata_or_reference"
    else:
        # When unrelated metadata conflicts, require positive contrast too.
        threshold={"strict":(.78,.055,.08),"balanced":(.72,.025,.055),
                   "exploratory":(.67,.0,.03)}[profile.get("evidence_threshold","balanced")]
        compatible = positive is not None and positive >= threshold[0] and (
            margin is None or margin >= threshold[1]
        ) and (support != "unrelated_metadata" or margin is not None and margin >= threshold[2])
        basis = "acoustic_reference_comparison" if compatible else "insufficient_genre_evidence"
    # Missing a measured modality is NOT secretly restored to full confidence.
    coverage = sum(1 for name in ("musicnn", "clap") if scores[name] is not None)/2
    fit = max(.0, min(1., positive or .0)) * (.75 + .25*coverage)
    if labeled:
        fit = max(fit, .79)
    if margin is not None:
        fit = max(0., min(1., fit + min(.08, max(-.12, margin * .3))))
    review = (reviews or {}).get(key) or {}
    if review.get("verdict") == "reject":
        compatible = False
        basis = "rejected_by_listener"
    elif review.get("verdict") == "accept":
        compatible = True
        basis = "accepted_by_listener"
    return {
        "genre": genre, "subgenre": subgenre,
        "evidence": {
            "plex_genre": list(info.get("genres") or []),
            "metadata_support": support,
            "musicnn_similarity": round(scores["musicnn"], 4) if scores["musicnn"] is not None else None,
            "clap_similarity": round(scores["clap"], 4) if scores["clap"] is not None else None,
            "subgenre_contrast": round(margin, 4) if margin is not None else None,
            "reference_support": basis,
            "review_status": review.get("verdict", "unreviewed"),
            "audio_coverage": coverage,
            "measured_genre_probability": None,
        },
        "fit": round(fit, 4),
        "compatible": compatible,
    }


class GenerationError(ValueError):
    pass


def _year(info):
    y=info.get("year")
    try:
        n=int(y)
        return n if 1800<=n<=2100 else None
    except (ValueError,TypeError):return None


def _duration(info):
    try:return int(info.get("duration_ms") or 0)
    except (ValueError,TypeError):return 0


def _has_live_evidence(info):
    text=" "+normalize(str(info.get("album",""))+" "+str(info.get("title","")))+" "
    return any(re.search(r"\b"+re.escape(cue)+r"\b",text) for cue in PERFORMANCE_CUES)


def _style_evidence(info,style):
    tags=[normalize(t) for t in info.get("genres",[]) if t]
    fields=[normalize(info.get("artist","")),normalize(info.get("album","")),
            normalize(info.get("title",""))]
    cues=STYLE_CUES.get(style,(style,))
    # Explicit genre tags are the primary evidence. Titles only provide weak evidence.
    if any(any(re.search(r"\b"+re.escape(normalize(cue))+r"\b",tag)
                   for cue in cues) for tag in tags):
        return 1.0,"Plex genre"
    if any(any(re.search(r"\b"+re.escape(normalize(cue))+r"\b",name)
                   for cue in cues) for name in fields):
        return .55,"artist/album/title"
    return None,None


def parse_prompt(prompt: str, *, count: int | None=None, minutes: int | None=None,
                 overrides: dict | None=None, genre: str | None=None,
                 subgenre: str | None=None, reference_track_ids=None,
                 negative_reference_track_ids=None, excluded_track_ids=None,
                 excluded_artists=None, excluded_albums=None, year_from=None,
                 year_to=None, evidence_threshold="balanced"):
    if not isinstance(prompt,str) or not 8<=len(prompt.strip())<=400:
        raise GenerationError("Describe the music in 8–400 characters.")
    prompt=prompt.strip()
    if evidence_threshold not in ("strict","balanced","exploratory"):
        raise GenerationError("Unknown genre evidence threshold.")
    inferred,evidence=infer(prompt,prompt,[],{}, {})
    structured_genre=_style_token(genre) if genre else ""
    structured_subgenre=_style_token(subgenre) if subgenre else ""
    if structured_subgenre and not structured_genre:
        raise GenerationError("Select a broad genre for the requested subgenre.")
    if structured_subgenre and structured_subgenre not in SUBGENRE_ALIASES:
        raise GenerationError("Unsupported subgenre: " + structured_subgenre)
    if structured_genre and structured_genre not in GENRE_ALIASES:
        raise GenerationError("Unsupported genre: " + structured_genre)
    if structured_subgenre and structured_genre != "jazz":
        raise GenerationError("The selected subgenre does not belong to this broad genre.")
    inferred_styles = list(inferred["styles"])
    if structured_genre and structured_genre not in inferred_styles:
        inferred_styles.append(structured_genre)
    inferred["styles"] = inferred_styles
    options={**inferred, **(overrides or {})}
    if structured_genre and structured_genre not in options["styles"]:
        options["styles"] = list(options["styles"]) + [structured_genre]
    intent=validate(options)
    years=YEAR_RANGE.findall(prompt)
    decades=YEAR_DECADE.findall(prompt)
    if len(years)>1:
        raise GenerationError("Use one year interval at a time.")
    if years:
        first,last=map(int,years[0])
        if first>last or last-first>75:
            raise GenerationError("Invalid or excessively broad year interval.")
        year_range=[first,last]
    elif decades:
        start=int(decades[0])
        year_range=[start,start+9]
    else:
        year_range=None
    if year_from is not None or year_to is not None:
        if not isinstance(year_from,int) or isinstance(year_from,bool) or not isinstance(year_to,int) or isinstance(year_to,bool):
            raise GenerationError("Both explicit year bounds must be integers.")
        if not 1800<=year_from<=year_to<=2100 or year_to-year_from>75:
            raise GenerationError("Invalid explicit release-year range.")
        year_range=[year_from,year_to]
    match=TRACK_COUNT.search(prompt)
    n=count if count is not None else (int(match.group(1)) if match else 24)
    if not isinstance(n,int) or isinstance(n,bool) or not MIN_TRACKS<=n<=MAX_TRACKS:
        raise GenerationError("Playlist length must be between 1 and 120 tracks.")
    minutes_match=MINUTES_COUNT.search(prompt)
    hours_match=HOUR_COUNT.search(prompt)
    duration=minutes if minutes is not None else (
        int(minutes_match.group(1)) if minutes_match else
        int(hours_match.group(1))*60 if hours_match else None)
    if duration is not None and (
        not isinstance(duration,int) or isinstance(duration,bool)
        or not 15<=duration<=MAX_MINUTES):
        raise GenerationError("Duration must be 15–900 minutes.")
    # Explicit year and 'live' cues are hard provenance filters in automatic
    # generation; missing metadata is excluded and reported.
    live_required=bool(LIVE_CUE.search(prompt))
    min_duration=8*60*1000 if LONG_CUE.search(prompt) else None
    # Music only: exclude introductions, interviews, applause etc.
    if (not intent["styles"] and not intent["moods"] and year_range is None
        and not live_required and min_duration is None and
        intent["energy"]=="adaptive" and intent["flow"]=="smooth"):
        raise GenerationError(
            "The brief has no verifiable musical criteria. Include a style, "
            "mood, period, live requirement or musical structure."
        )
    criteria={
        "years":year_range, "live_required":live_required,
        "minimum_duration_ms":min_duration,
        "count":n,"target_minutes":duration,
        "genre":structured_genre,"subgenre":structured_subgenre,
        "reference_track_ids":_bounded_ids(reference_track_ids,"reference_track_ids"),
        "negative_reference_track_ids":_bounded_ids(negative_reference_track_ids,"negative_reference_track_ids"),
        "excluded_track_ids":_bounded_ids(excluded_track_ids,"excluded_track_ids"),
        "require_exact_count":bool(structured_genre),
        "excluded_artists":[normalize(a) for a in (excluded_artists or [])],
        "excluded_albums":[normalize(a) for a in (excluded_albums or [])],
        "evidence_threshold":evidence_threshold,
        "selection_policy_version":SELECTION_POLICY_VERSION,
    }
    uncertain=[]
    if year_range:
        uncertain.append("Years use Plex album-release metadata, not verified concert/recording dates.")
    if live_required:
        uncertain.append("Live identity is verified only by explicit album/track title cues.")
    if any(cue in normalize(prompt) for cue in ("club ambience","club atmosphere","clubsfeer","audience atmosphere","audience chatter","club sound")):
        uncertain.append("Club ambience and audience sound are not directly measured.")
    if min_duration:
        uncertain.append("Long track duration does not prove improvisation.")
    if not intent["styles"]:
        uncertain.append("No recognized musical style; ranking is driven by available metadata/audio only.")
    return {"prompt":prompt,"identity":intent,"criteria":criteria,
            "interpretation":{"confidence":"rule_based","warnings":uncertain,
              "explicit_styles":intent["styles"],"metadata_year_kind":"album_release_year"}}


def _constraints(info,request):
    criteria=request["criteria"]
    if str(info.get("key") or "") in criteria.get("excluded_track_ids", []):
        return "user_excluded"
    if normalize(info.get("artist","")) in criteria.get("excluded_artists",[]):
        return "excluded_artist"
    if normalize(info.get("album","")) in criteria.get("excluded_albums",[]):
        return "excluded_album"
    if not automatically_rankable(info):return "spoken_or_applause"
    if criteria["years"]:
        year=_year(info)
        if year is None:return "missing_year"
        if year<criteria["years"][0] or year>criteria["years"][1]:return "outside_years"
    if criteria["live_required"] and not _has_live_evidence(info):
        return "live_unverified"
    if criteria["minimum_duration_ms"] is not None:
        duration=_duration(info)
        if duration==0:return "missing_duration"
        if duration<criteria["minimum_duration_ms"]:return "too_short"
    return None


def _initial_value(info,ff,intent):
    styles=intent["styles"]
    style_scores=[score for style in styles
                  if (score:=_style_evidence(info,style)[0]) is not None]
    if styles and not style_scores:
        return None
    style=(max(style_scores) if style_scores else .5)
    intrinsic,evidence=_intent_score(ff,info,intent,None)
    # With a mood-only brief, do not fall back to alphabetical metadata when
    # no mood/energy measurements exist for this candidate.
    if not styles and intent["moods"] and intrinsic is None:
        return None
    return .68*style+.32*(intrinsic if intrinsic is not None else .5)


def _selection_center(selected, features):
    """Compute once for each selection step, not once per catalogue candidate."""
    recent=[features[key] for key in selected[-12:] if key in features]
    return {
        "musicnn":_centroid([f["musicnn"] for f in recent if f.get("musicnn") is not None]),
        "clap":_centroid([f["clap"] for f in recent if f.get("clap") is not None]),
        "energy":_mean([f["energy"] for f in recent if f.get("energy") is not None]),
    }


def _intrinsic(candidate, selected, library, features, intent, previous, base, center=None):
    """Fast, complete-catalogue scoring with one cached contextual centroid."""
    ff=features.get(candidate,{})
    info=library[candidate]
    static=base[candidate]
    artist=info["artist"]
    album=info["album_key"]
    a_count=sum(library[k]["artist"]==artist for k in selected)
    b_count=sum(library[k]["album_key"]==album for k in selected)
    mode=intent["variation"]
    concentration=(.025 if mode=="focused" else .11 if mode=="balanced" else .16)*a_count
    concentration+=.055*b_count
    value=static-concentration
    if selected and center:
        match=_neighbor_score(ff,center)
        if match is not None:
            value=.70*value+.30*match
    if previous in features:
        neighbor=_neighbor_score(ff,features[previous])
        if neighbor is not None:
            if intent["flow"]=="smooth":value+=.12*(neighbor-.5)
            elif intent["flow"]=="varied":value+=.09*(1-abs(neighbor-.70)/.7-.5)
    return value


def _sequence(keys,lib,ff,intent):
    """Bounded greedy ordering with local adjacent swaps, scored on actual transitions."""
    if len(keys)<=2:return list(keys)
    remaining=set(keys)
    energies=[ff[k]["energy"] for k in keys if k in ff and ff[k].get("energy") is not None]
    level=sum(energies)/len(energies) if energies else .5
    def arc_target(pos):
        if intent["flow"]=="arc":
            return clamp(level-.13+.3*math.sin(math.pi*pos/max(1,len(keys)-1)))
        return level
    def transition(first,second,pos):
        f1,f2=ff.get(first,{}),ff.get(second,{})
        adjacent=_neighbor_score(f1,f2)
        energy1,energy2=f1.get("energy"),f2.get("energy")
        smooth_energy=(1-abs(energy1-energy2)) if energy1 is not None and energy2 is not None else None
        tempo1,tempo2=f1.get("tempo"),f2.get("tempo")
        tempo_match=(clamp(1-min(abs(math.log2((tempo1*m)/tempo2)) for m in (.5,1,2)))
                     if tempo1 and tempo2 and tempo1>0 and tempo2>0 else None)
        shared=set(f1.get("mood") or {})&set(f2.get("mood") or {})
        mood_match=(clamp(1-_mean([abs(f1["mood"][k]-f2["mood"][k]) for k in shared]))
                    if shared else None)
        pairs=[(v,w) for v,w in ((adjacent,.70),(smooth_energy,.14),
                                 (tempo_match,.10),(mood_match,.06)) if v is not None]
        continuity=sum(v*w for v,w in pairs)/sum(w for _,w in pairs) if pairs else .5
        # Penalize adjacent duplicates of the same recording session.
        if lib[first]["album_key"]==lib[second]["album_key"]:
            continuity-=.06
        elif lib[first]["artist"]==lib[second]["artist"]:
            continuity-=.025
        if adjacent is not None and adjacent>.975 and intent["variation"]!="focused":
            continuity-=.035  # Avoid an entire sequence of virtually identical recordings.
        if intent["flow"]=="arc":
            gradient=clamp(1-abs(energy2-arc_target(pos))/.7) if energy2 is not None else .5
            return clamp(.65*continuity+.35*gradient)
        if intent["flow"]=="varied":
            return clamp(1-abs(continuity-.74)/.7)
        return clamp(continuity)
    def head(k):
        en=ff.get(k,{}).get("energy")
        if en is None:return 0
        return -abs(en-arc_target(0))
    first=max(keys,key=lambda k:(head(k),-keys.index(k)))
    out=[first]
    remaining.remove(first)
    while remaining:
        pos=len(out)
        candidate=max(sorted(remaining),key=lambda k:(
            transition(out[-1],k,pos),
            -sum(lib[x]["artist"]==lib[k]["artist"] for x in out[-3:]),
            -keys.index(k)))
        out.append(candidate)
        remaining.remove(candidate)
    # Small adjacent 2-opt pass, preserving melodic continuity and arc.
    for _ in range(2):
        improved=False
        for i in range(1,len(out)-1):
            a=out[i-1]
            b,c=out[i],out[i+1]
            d=out[i+2] if i+2<len(out) else None
            old=transition(a,b,i)+transition(b,c,i+1)
            new=transition(a,c,i)+transition(c,b,i+1)
            if d is not None:
                old+=transition(c,d,i+2)
                new+=transition(b,d,i+2)
            if new>old+.02:
                out[i],out[i+1]=out[i+1],out[i]
                improved=True
        if not improved:break
    return out


def transition_diagnostics(keys, decoded):
    """Same sequence diagnostic used during generation and after every edit."""
    transitions=[]
    for index,key in enumerate(keys):
        if index==0:continue
        prev=keys[index-1]
        prior=decoded.get(prev,{})
        current=decoded.get(key,{})
        similarity=_neighbor_score(prior,current)
        e1,e2=prior.get("energy"),current.get("energy")
        jump=abs(e1-e2) if e1 is not None and e2 is not None else None
        tempo1,tempo2=prior.get("tempo"),current.get("tempo")
        tempo_delta=(min(abs(math.log2((tempo1*m)/tempo2)) for m in (.5,1,2))
                     if tempo1 and tempo2 and tempo1>0 and tempo2>0 else None)
        transitions.append({"from_id":prev,"to_id":key,
            "audio_similarity":round(similarity,4) if similarity is not None else None,
            "energy_jump":round(jump,4) if jump is not None else None,
            "tempo_octave_gap":round(tempo_delta,4) if tempo_delta is not None else None,
            "warning":bool((similarity is not None and similarity<.57)
                        or (jump is not None and jump>.35))})
    return transitions


def diagnose(keys,library,feature_cache,intent,request=None):
    from .curation_impact import measure
    features=features_for({k:feature_cache[k] for k in keys if k in feature_cache})
    metrics=measure(keys,library,features,intent)
    artist_counts=Counter(library[k]["artist"] for k in keys)
    album_counts=Counter(library[k]["album_key"] for k in keys)
    flags=[]
    if len(keys)!=len(set(keys)):flags.append("Duplicate track IDs")
    if len(keys)>=6 and max(artist_counts.values())/len(keys)>.4 and intent["variation"]!="focused":
        flags.append("High artist concentration")
    if len(keys)>=6 and max(album_counts.values())/len(keys)>.3:
        flags.append("Several tracks from one album")
    if metrics["coherence"]["status"]=="insufficient":
        flags.append("Limited AudioMuse coverage: musical coherence cannot be established")
    if request and request["criteria"]["years"]:
        flags.append("Period checks rely on Plex release-year metadata")
    if request and request["criteria"]["live_required"]:
        flags.append("Live checks rely on album/track title metadata")
    total_minutes=round(sum(_duration(library[k]) for k in keys)/60000,1)
    return {"metrics":metrics,"flags":flags,
            "transitions":transition_diagnostics(keys,features),
            "tracks":len(keys),"duration_minutes":total_minutes,
            "unique_artists":len(artist_counts),
            "unique_albums":len(album_counts),
            "analyzed":metrics["counts"]["audio_analyzed"],
            "unmeasured_tracks":len(keys)-metrics["counts"]["audio_analyzed"]}


def generate(request,library,feature_cache, *, reviews=None):
    """Select and sequence across the entire catalogue using observed evidence."""
    identity=request["identity"]
    criteria=request["criteria"]
    excluded=Counter()
    ff=features_for(feature_cache)
    genre=criteria.get("genre") or ""
    subgenre=criteria.get("subgenre") or ""
    profile=(genre_profiles(library,ff,genre=genre,subgenre=subgenre,
                           reference_track_ids=criteria.get("reference_track_ids",[]),
                           negative_reference_track_ids=criteria.get("negative_reference_track_ids",[]),
                           evidence_threshold=criteria.get("evidence_threshold","balanced"))
             if genre else None)
    base={}
    evidence={}
    for key,info in sorted(library.items()):
        reason=_constraints(info,request)
        if reason:
            excluded[reason]+=1
            continue
        if profile:
            signal=genre_evidence(key,info,ff,profile,reviews=reviews)
            if not signal["compatible"]:
                excluded["genre_evidence_insufficient"]+=1
                continue
            intrinsic,_ = _intent_score(ff.get(key,{}),info,identity,None)
            value=.76*signal["fit"]+.24*(intrinsic if intrinsic is not None else .5)
            signal["ranking_components"]={
                "genre_fit":signal["fit"],
                "intent_fit":round(intrinsic,4) if intrinsic is not None else None,
                "weighted_fit":round(value,4),
                "weights":{"genre_fit":.76,"intent_fit":.24},
                "intent_unmeasured":intrinsic is None,
                "meaning":"Relative evidence, not a calibrated genre probability",
            }
            evidence[key]=signal
            base[key]=round(value,6)
        else:
            value=_initial_value(info,ff.get(key,{}),identity)
            if value is not None:base[key]=value
            else:excluded["style_unverified"]+=1
    if not base:
        raise GenerationError("No tracks satisfy the explicit criteria. Broaden the date, live or style requirements.")
    ranked=sorted(base,key=lambda k:(-base[k],normalize(library[k]["artist"]),
                                     normalize(library[k]["title"]),k))
    # Do NOT truncate this list. Every eligible Plex track is a candidate.
    wanted=criteria["count"]
    minutes_target=criteria["target_minutes"]
    if criteria.get("require_exact_count") and len(ranked)<wanted:
        raise GenerationError(
            f"Only {len(ranked)} of {wanted} tracks have defensible genre evidence. "
            "Select verified references or relax the genre constraints."
        )
    selected=[]
    elapsed=0
    remaining=set(ranked)
    rank_index={key:i for i,key in enumerate(ranked)}
    while remaining and len(selected)<wanted:
        last=selected[-1] if selected else None
        center=_selection_center(selected,ff) if selected else None
        # Evaluate every remaining candidate; no arbitrary shortlist cutoff.
        best=max(sorted(remaining),key=lambda k:(
            _intrinsic(k,selected,library,ff,identity,last,base,center),-rank_index[k]))
        duration=_duration(library[best])
        if minutes_target and elapsed>=minutes_target*60000 and selected:
            break
        selected.append(best)
        remaining.remove(best)
        elapsed+=duration
    if not selected:raise GenerationError("No eligible music remains after filtering.")
    if criteria.get("require_exact_count") and len(selected)<wanted:
        raise GenerationError(
            f"Duration constraints produced only {len(selected)} of {wanted} tracks. "
            "Increase target minutes or remove the duration cap."
        )
    ordered=_sequence(selected,library,ff,identity)
    diagnostics=diagnose(ordered,library,feature_cache,identity,request)
    warnings=list(request["interpretation"]["warnings"])
    if len(ordered)<wanted and not minutes_target:
        warnings.append(f"Only {len(ordered)} of {wanted} tracks meet the verified criteria.")
    if minutes_target and elapsed<minutes_target*60000:
        warnings.append("The requested duration could not be reached with eligible tracks.")
    if diagnostics["unmeasured_tracks"]:
        warnings.append("Some track placements depend on metadata because AudioMuse measurements are unavailable; see evidence for each track.")
    tracks=[{"id":key,"artist":library[key]["artist"],"album":library[key]["album"],
             "title":library[key]["title"],"year":_year(library[key]),
             "duration_ms":_duration(library[key]),"genres":library[key].get("genres",[]),
             "audio_analyzed":key in ff and any(ff[key].get(v) is not None for v in AUDIO_VECTORS),
             "live_title_evidence":_has_live_evidence(library[key]),
             "review_flags":review_track(library[key]),
             "genre_evidence":evidence.get(key)} for key in ordered]
    selection={"catalogue":len(library),"initial_candidates":len(library)-sum(
                   count for reason,count in excluded.items()
                   if reason not in ("genre_evidence_insufficient","style_unverified")),
               "eligible":len(base),"shortlisted":len(ranked),
               "evaluated":len(ranked),"excluded":dict(excluded),
               "target":wanted,"target_minutes":minutes_target,
               "selection_policy_version":SELECTION_POLICY_VERSION,
               "prior_genre_reviews":len(reviews or {})}
    return {"keys":ordered,"tracks":tracks,"diagnostics":diagnostics,
            "genre_evidence":{key:evidence[key] for key in ordered if key in evidence},
            "genre_profile":profile,"selection":selection,"warnings":warnings}
