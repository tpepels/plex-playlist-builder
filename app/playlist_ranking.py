"""Playlist-aware ranking over the entire Plex catalogue.

Candidates are judged by multiple existing musical neighborhoods, declared
playlist intent, diversity and insertion context. Unanalyzed candidates have
no fabricated quality score. This module cannot edit Plex or YAML.
"""
from __future__ import annotations

from collections import Counter
import math

from .insights import normalize
from .curation_review_rules import automatically_rankable, review_track
from .playlist_curator import (
    CurationConflict, MAX_CANDIDATES, cosine, features_for, _centroid, _mean,
)

ENERGY_LEVELS = {"low": .25, "medium": .50, "high": .76}
MOOD_FEATURES = {
    "relaxed": ("relaxed",1), "warm":("relaxed",1), "intimate":("relaxed",1),
    "reflective":("relaxed",1), "melancholic":("sad",1),
    "happy":("happy",1), "energetic":("aggressive",1),
    "danceable":("danceable",1), "aggressive":("aggressive",1),
}
SCORE_WEIGHTS = {
    "focused": {"fit": .48, "diversity": .08, "flow": .21, "intent": .23},
    "balanced":{"fit": .36, "diversity": .16, "flow": .24, "intent": .24},
    "eclectic":{"fit": .29, "diversity": .25, "flow": .24, "intent": .22},
}
SHORTLIST = 150


def clamp(x):
    return max(0., min(1., x))


def _cue(text):
    norm=normalize(text)
    return any(w in norm.split() for w in ("live","concert","unplugged")) or (
        "village vanguard" in norm or "at the" in norm and "club" in norm)


def _pick(seq, limit=12):
    """Stratified anchors: early, middle and late sections all contribute."""
    if len(seq)<=limit:
        return seq
    return [seq[round(i*(len(seq)-1)/(limit-1))] for i in range(limit)]


def _sample_fit(candidate, sampled, centers, mode):
    """Compare with all represented musical subgroups in both embedding spaces."""
    scores = []
    for field, weight in (("musicnn", .7), ("clap", .3)):
        vector = candidate.get(field)
        if vector is None:
            continue
        comps = [cosine(vector, feat.get(field)) for feat in sampled]
        comps = sorted((clamp((v+1)/2) for v in comps if v is not None), reverse=True)
        if not comps:
            continue
        best = _mean(comps[:min(3,len(comps))])
        raw = cosine(vector, centers.get(field))
        typical = clamp((raw+1)/2) if raw is not None else best
        if mode == "focused":
            value = .65*typical+.35*best
        elif mode == "eclectic":
            # Reward fit with at least one musical neighborhood without
            # imposing homogeneity across an intentionally mixed playlist.
            novelty = clamp(1-abs(best-.77)/.55)
            value = .55*best+.25*typical+.20*novelty
        else:
            value = .48*typical+.52*best
        scores.append((value,weight))
    return sum(v*w for v,w in scores)/sum(w for _,w in scores) if scores else None

def _concentration(keys, library):
    if not keys:
        return 0., 0.
    a=Counter(library[k]["artist"] for k in keys)
    b=Counter(library[k]["album_key"] for k in keys)
    return max(a.values())/len(keys), max(b.values())/len(keys)


def _diversity(keys, new_key, library, mode, anchor_artist=None):
    prior = keys[:]
    after = keys+[new_key]
    a1,b1=_concentration(prior,library)
    a2,b2=_concentration(after,library)
    artist=library[new_key]["artist"]
    count=sum(library[k]["artist"]==artist for k in prior)
    album_count=sum(library[k]["album_key"]==library[new_key]["album_key"] for k in prior)
    if anchor_artist:
        # A named-artist playlist is allowed to concentrate on that artist.
        return .92 if normalize(artist)==anchor_artist else .18
    if mode=="focused":
        return clamp(.85-.12*max(0,count-2)-.05*album_count)
    gain = clamp(.5+3*(a1-a2)+2*(b1-b2))
    novelty = 1 if count==0 else .5 if count==1 else .15
    album = 1 if album_count==0 else .2
    if mode=="eclectic":
        return clamp(.3*gain+.5*novelty+.2*album)
    return clamp(.45*gain+.4*novelty+.15*album)


def _intent_score(f, info, intent, observed_energy):
    available=[]
    reasons=[]
    mode=intent["energy"]
    desired=ENERGY_LEVELS.get(mode)
    if desired is None:
        desired=observed_energy
    if desired is not None and f.get("energy") is not None:
        available.append((clamp(1-abs(f["energy"]-desired)/.65), .4))
        reasons.append("energy target")
    mood_scores=[]
    for label in intent["moods"]:
        if label not in MOOD_FEATURES:
            continue
        feature, direction = MOOD_FEATURES[label]
        value=f.get("mood",{}).get(feature)
        if value is not None:
            mood_scores.append(clamp(value if direction==1 else 1-value))
    if mood_scores:
        available.append((_mean(mood_scores), .4))
        reasons.append("declared mood")
    if intent["live"] != "neutral":
        # Album/track title cues indicate a positive match only; absence of
        # the keyword cannot prove a studio recording.
        if _cue(info["album"]+" "+info["title"]):
            available.append((1. if intent["live"]=="prefer" else 0., .2))
            reasons.append("explicit live-release cue")
    declared = [normalize(style) for style in intent["styles"] if normalize(style)]
    genres = [normalize(g) for g in (info.get("genres") or []) if normalize(g)]
    if declared and genres:
        direct = any(d == g or d in g.split() or g in d.split()
                     for d in declared for g in genres)
        # Plex metadata is an imperfect taxonomy; mismatch gets a soft
        # penalty, never a definitive genre exclusion.
        available.append((1. if direct else .35, .5))
        reasons.append("Plex genre metadata")
    if not available:
        return None, []
    return sum(v*w for v,w in available)/sum(w for _,w in available), reasons


def _neighbor_score(a, b):
    if a is None or b is None:
        return None
    sims = []
    for kind in ("musicnn","clap"):
        v=cosine(a.get(kind), b.get(kind))
        if v is not None:
            sims.append(clamp((v+1)/2))
    audio=_mean(sims)
    e1,e2=a.get("energy"),b.get("energy")
    energy=clamp(1-abs(e1-e2)) if e1 is not None and e2 is not None else None
    if audio is None:
        return energy
    return .78*audio+.22*energy if energy is not None else audio


def _flow(candidate, pos, sequence, ff, intent, observed_energy):
    left=ff.get(sequence[pos-1]) if pos>0 else None
    right=ff.get(sequence[pos+1]) if pos+1<len(sequence) else None
    adjacent=[v for v in (_neighbor_score(candidate,left),_neighbor_score(candidate,right)) if v is not None]
    if not adjacent:
        return None
    continuity=_mean(adjacent)
    if intent["flow"]=="varied":
        # Allow transitions, but penalize disconnection.
        continuity=clamp(1-abs(continuity-.75)/.6)
    if intent["flow"]=="arc" and candidate.get("energy") is not None:
        center=ENERGY_LEVELS.get(intent["energy"], observed_energy if observed_energy is not None else .5)
        arc=center-.1 + .24*math.sin(math.pi*pos/max(1,len(sequence)-1))
        continuity=.65*continuity+.35*clamp(1-abs(candidate["energy"]-arc)/.65)
    return continuity


def _profile_context(keys, library, ff, intent, target):
    base=[key for key in keys if key!=target]
    samplekeys=_pick([k for k in base if k in ff and (
        ff[k].get("musicnn") is not None or ff[k].get("clap") is not None)],12)
    sampled=[ff[k] for k in samplekeys]
    centers={field:_centroid([f[field] for f in sampled if f.get(field) is not None])
             for field in ("musicnn","clap")}
    energies=[ff[k]["energy"] for k in base if k in ff and ff[k].get("energy") is not None]
    observed_energy=_mean(energies)
    title=normalize(intent["purpose"])
    artist_names={normalize(library[k]["artist"]) for k in keys if k in library}
    explicit_artist=next((artist for artist in artist_names if artist and
                          len(artist)>=5 and (" "+artist+" ") in (" "+title+" ")),None)
    return base,sampled,centers,observed_energy,explicit_artist


def _score_candidate(key, info, f, keys, lib, ff, intent, target, ctx,
                     *, detailed, weights=None):
    base,sampled,centers,energy,artist_anchor=ctx
    fit=_sample_fit(f,sampled,centers,intent["variation"])
    if fit is None:
        return {"match":None,"confidence":"unscored","evidence":[],
                "reasons":["No comparable audio embedding"],"factors":{},
                "suggested_after_id":None}
    diversity=_diversity(base,key,lib,intent["variation"],artist_anchor)
    intention, reasons = _intent_score(f,info,intent,energy)
    position=None
    flow=None
    if detailed:
        if target is not None:
            pos=keys.index(target)
            sequence=list(keys)
            sequence[pos]=key
            flow=_flow(f,pos,sequence,ff,intent,energy)
        else:
            for pos in range(len(base)+1):
                sequence=base[:pos]+[key]+base[pos:]
                value=_flow(f,pos,sequence,ff,intent,energy)
                if value is not None and (flow is None or value>flow):
                    position=pos
                    flow=value
    else:
        if target:
            pos=keys.index(target)
            sequence=list(keys);sequence[pos]=key
            flow=_flow(f,pos,sequence,ff,intent,energy)
    if detailed and target is None and position is None:
        position=len(base)   # No measured ordering signal: append, never prepend by accident.
    factors={"fit":fit,"diversity":diversity,"flow":flow,"intent":intention}
    weights=weights or SCORE_WEIGHTS[intent["variation"]]
    usable=[(key,value) for key,value in factors.items() if value is not None]
    score=sum(value*weights[k] for k,value in usable)/sum(weights[k] for k,_ in usable)
    evidence=["audio neighborhoods","artist/album diversity"]
    if flow is not None:evidence.append("playlist sequence")
    if intention is not None:evidence.extend(reasons)
    # Does not claim strong confidence without representative measured coverage.
    coverage=len([k for k in base if k in ff and (
        ff[k].get("musicnn") is not None or ff[k].get("clap") is not None)])/max(1,len(base))
    conf="high" if coverage>=.75 and len(sampled)>=12 and f.get("clap") is not None else (
        "medium" if coverage>=.6 and len(sampled)>=6 else "low")
    summaries=[]
    if fit>=.8:summaries.append("Fits an existing musical subgroup")
    elif fit<.62:summaries.append("Distinct from analyzed musical subgroups")
    if diversity>=.75:summaries.append("Adds artist or album variety")
    if diversity<.3:summaries.append("Increases repetition or concentration")
    if flow is not None and flow>=.8:summaries.append("Fits neighboring tracks")
    elif flow is not None and flow<.55:summaries.append("Unusual transition")
    if intention is not None and intention>=.75:summaries.append("Matches stated mood or energy")
    if not summaries:summaries.append("Moderate fit across available dimensions")
    return {
        "match":round(clamp(score),4), "confidence":conf,
        "evidence":evidence,"reasons":summaries,
        "factors":{k:round(v,3) if v is not None else None for k,v in factors.items()},
        "suggested_after_id":base[position-1] if position is not None and position>0
                            else None if position==0 else None,
    }


def rank_candidates(keys, library, cache, identity, *, target=None, query="",limit=20,
                    weights=None, feedback=None, genre_signals=None, rejected_ids=None):
    if not 1<=limit<=MAX_CANDIDATES:
        raise ValueError("Limit must be between 1 and 30.")
    if target is not None and target not in keys:
        raise CurationConflict("Target track is not in this playlist.")
    if len(query)>120:
        raise ValueError("Search query is too long.")
    ff=features_for(cache)
    intent=identity["values"] if "values" in identity else identity
    ctx=_profile_context(keys,library,ff,intent,target)
    feedback=feedback or {}
    rejected_ids=set(rejected_ids or [])
    query_norm=normalize(query)
    pool=[]
    for key,info in library.items():
        if key in keys or key in rejected_ids:continue
        if genre_signals is not None and not genre_signals.get(key,{}).get("compatible"):
            continue
        # Concert introductions and applause are in music libraries but are
        # not defensible automatic recommendations for musical playlists.
        if not query_norm and not automatically_rankable(info):
            continue
        if query_norm and query_norm not in normalize(" ".join((
            info["artist"],info["album"],info["title"]))):
            continue
        f=ff.get(key,{})
        ranked=_score_candidate(key,info,f,keys,library,ff,intent,target,ctx,
                                detailed=False,weights=weights)
        if genre_signals is not None and ranked["match"] is not None:
            ranked["match"]=round(clamp(.68*genre_signals[key]["fit"]+
                                          .32*ranked["match"]),4)
        pool.append((key,info,ranked))
    pool.sort(key=lambda x:(
        x[2]["match"] is None,
        -(x[2]["match"] if x[2]["match"] is not None else -1),
        normalize(x[1]["artist"]),normalize(x[1]["album"]),
        normalize(x[1]["title"]),x[0]))
    shortlisted=pool[:max(limit,SHORTLIST)]
    results=[]
    for key,info,_ in shortlisted:
        item=_score_candidate(key,info,ff.get(key,{}),keys,library,ff,intent,target,ctx,
                              detailed=True,weights=weights)
        if genre_signals is not None:
            item["genre_evidence"]=genre_signals[key]
            item["ranking_components"]={"genre":genre_signals[key]["fit"],
                                         "playlist_fit":item["match"]}
            if item["match"] is not None:
                item["match"]=round(clamp(.68*genre_signals[key]["fit"]+
                                             .32*item["match"]),4)
        flags=review_track(info)
        item["review_flags"]=flags
        if not automatically_rankable(info):
            item["match"]=None
            item["confidence"]="unscored"
            item["reasons"]=["Likely spoken introduction/applause; review manually"]
            item["evidence"]=[]
        human=feedback.get((target or "",key))
        base_score=item["match"]
        if human and base_score is not None:
            # Keep model evidence separate and visible. A previous explicit
            # rejection must not silently be proposed as the top improvement.
            adjustment=.08 if human["verdict"]=="accept" else -.18
            item["match"]=round(clamp(base_score+adjustment),4)
            item["model_match"]=base_score
            item["feedback_adjustment"]=round(adjustment,3)
            item["human_review"]={"verdict":human["verdict"],"issue":human["issue"]}
            item["reasons"].append("Previous listening verdict: "+human["verdict"])
        results.append({
            "id":key, "artist":info["artist"],"album":info["album"],"title":info["title"],
            "analyzed":key in ff and (ff[key].get("musicnn") is not None or
                                 ff[key].get("clap") is not None),
            **item
        })
    results.sort(key=lambda x:(
        bool(x.get("human_review") and x["human_review"]["verdict"]=="reject"),
        x["match"] is None,
        -(x["match"] if x["match"] is not None else -1),
        normalize(x["artist"]),normalize(x["album"]),normalize(x["title"]),x["id"]))
    return results[:limit]
