"""Music-prompt-driven generation of ordered playlists from the full Plex catalogue.

All claims are constrained to available Plex metadata and observed AudioMuse
features. The deterministic parser does not silently infer concert recording
dates, instrumentation or atmosphere. No Plex or YAML mutation happens here.
"""
from __future__ import annotations

from collections import Counter
import math
import re

from .insights import normalize
from .playlist_identity import infer, validate, STYLE_CUES
from .playlist_curator import features_for, cosine
from .playlist_ranking import _intent_score, _neighbor_score, clamp, _score_candidate, _profile_context
from .curation_review_rules import automatically_rankable, review_track
from .curation_impact import measure

MAX_TRACKS=120
MIN_TRACKS=1
MAX_MINUTES=900
MAX_POOL=500

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
                 overrides: dict | None=None):
    if not isinstance(prompt,str) or not 8<=len(prompt.strip())<=400:
        raise GenerationError("Describe the music in 8–400 characters.")
    prompt=prompt.strip()
    inferred,evidence=infer(prompt,prompt,[],{}, {})
    intent=validate(inferred if overrides is None else {**inferred,**overrides})
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
    criteria={
        "years":year_range, "live_required":live_required,
        "minimum_duration_ms":min_duration,
        "count":n,"target_minutes":duration,
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
    # Melody and ambience features contribute only if actually measured.
    return .68*style+.32*(intrinsic if intrinsic is not None else .5)


def _intrinsic(candidate, selected, library, features, intent, previous, base):
    ff=features.get(candidate,{})
    info=library[candidate]
    static=base[candidate]
    artist=info["artist"]
    album=info["album_key"]
    a_count=sum(library[k]["artist"]==artist for k in selected)
    b_count=sum(library[k]["album_key"]==album for k in selected)
    mode=intent["variation"]
    # Intentional focused playlists may repeat a performer. Repetition of
    # an album, however, still receives a moderate penalty.
    concentration=(.025 if mode=="focused" else .11 if mode=="balanced" else .16)*a_count
    concentration+=.055*b_count
    value=static-concentration
    if selected:
        anchor=selected[-min(len(selected),12):]
        ctx=_profile_context(anchor,library,features,intent,None)
        evaluated=_score_candidate(candidate,info,ff,anchor,library,features,intent,
                                   None,ctx,detailed=False)
        if evaluated["match"] is not None:
            value=.50*value+.50*evaluated["match"]
    if previous in features:
        neighbor=_neighbor_score(ff,features[previous])
        if neighbor is not None:
            if intent["flow"]=="smooth": value+=.12*(neighbor-.5)
            elif intent["flow"]=="varied": value+=.09*(1-abs(neighbor-.70)/.7-.5)
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
        energy=f2.get("energy")
        gradient=clamp(1-abs(energy-arc_target(pos))/.7) if energy is not None else None
        if intent["flow"]=="arc":
            return (.55*(adjacent if adjacent is not None else .5)+
                    .45*(gradient if gradient is not None else .5))
        if adjacent is None:return .5
        if intent["flow"]=="varied":
            return clamp(1-abs(adjacent-.74)/.7)
        return adjacent
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
            "tracks":len(keys),"duration_minutes":total_minutes,
            "unique_artists":len(artist_counts),
            "unique_albums":len(album_counts),
            "analyzed":metrics["counts"]["audio_analyzed"],
            "unmeasured_tracks":len(keys)-metrics["counts"]["audio_analyzed"]}


def generate(request,library,feature_cache):
    """Select candidates from the full catalogue and sequence the complete draft."""
    identity=request["identity"]
    excluded=Counter()
    pool=[]
    for key,info in library.items():
        reason=_constraints(info,request)
        if reason:
            excluded[reason]+=1
            continue
        pool.append(key)
    ff=features_for({key:feature_cache[key] for key in pool if key in feature_cache})
    base={}
    for key in pool:
        value=_initial_value(library[key],ff.get(key,{}),identity)
        if value is not None:base[key]=value
        else:excluded["style_unverified"]+=1
    if not base:
        raise GenerationError("No tracks satisfy the explicit criteria. Broaden the date, live or style requirements.")
    ranked=sorted(base,key=lambda k:(-base[k],normalize(library[k]["artist"]),
                                     normalize(library[k]["title"]),k))
    rank_index={key:i for i,key in enumerate(ranked)}
    # Preserve cross-genre candidate representation: the initial shortlist is
    # not allowed to collapse into a single popular subgenre.
    shortlist=ranked[:MAX_POOL]
    if len(ranked)>MAX_POOL and len(identity["styles"])>1:
        chosen=set(shortlist)
        for style in identity["styles"]:
            stylistic=[k for k in ranked if _style_evidence(library[k],style)[0] is not None]
            for key in stylistic[:max(20,MAX_POOL//(3*len(identity["styles"])))]:
                if key not in chosen:
                    shortlist.append(key);chosen.add(key)
    wanted=request["criteria"]["count"]
    minutes_target=request["criteria"]["target_minutes"]
    selected=[]
    elapsed=0
    remaining=set(shortlist)
    while remaining and len(selected)<wanted:
        last=selected[-1] if selected else None
        best=max(remaining,key=lambda k:(
            _intrinsic(k,selected,library,ff,identity,last,base),
            -rank_index[k]))
        duration=_duration(library[best])
        if minutes_target and elapsed>=minutes_target*60000 and selected:
            break
        selected.append(best)
        remaining.remove(best)
        elapsed+=duration
    if not selected:raise GenerationError("No eligible music remains after filtering.")
    ordered=_sequence(selected,library,ff,identity)
    diagnostics=diagnose(ordered,library,feature_cache,identity,request)
    warnings=list(request["interpretation"]["warnings"])
    if len(ordered)<wanted and not minutes_target:
        warnings.append(f"Only {len(ordered)} of {wanted} tracks meet the verified criteria.")
    if minutes_target and elapsed<minutes_target*60000:
        warnings.append("The requested duration could not be reached with eligible tracks.")
    if diagnostics["unmeasured_tracks"]:
        warnings.append("Some track placements depend on metadata because AudioMuse measurements are unavailable.")
    tracks=[{"id":key,"artist":library[key]["artist"],"album":library[key]["album"],
             "title":library[key]["title"],"year":_year(library[key]),
             "duration_ms":_duration(library[key]),"genres":library[key].get("genres",[]),
             "audio_analyzed":key in ff and any(ff[key].get(v) is not None for v in AUDIO_VECTORS),
             "live_title_evidence":_has_live_evidence(library[key]),
             "review_flags":review_track(library[key])} for key in ordered]
    return {"keys":ordered,"tracks":tracks,"diagnostics":diagnostics,
            "selection":{"catalogue":len(library),"initial_candidates":len(pool),
                         "eligible":len(base),"shortlisted":len(shortlist),
                         "excluded":dict(excluded),"target":wanted,
                         "target_minutes":minutes_target},
            "warnings":warnings}
