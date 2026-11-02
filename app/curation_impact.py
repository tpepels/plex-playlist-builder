"""Paired, coverage-aware before/after assessment of actual playlist sequences.

Values are bounded heuristics, *not* calibrated musical quality probabilities.
No missing audio feature is imputed. Pairwise changes are only comparable when
both configurations clear per-metric coverage gates. No side effects.
"""
from __future__ import annotations

from collections import Counter
import math

from .playlist_curator import cosine, features_for
from .playlist_ranking import ENERGY_LEVELS, MOOD_FEATURES, clamp

MIN_AUDIO = 6
MIN_COVERAGE = .60
MAX_COHERENCE_SAMPLE = 80
NAMES = ("coherence", "variation", "energy", "flow", "intent")


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _sample(ids, cap=MAX_COHERENCE_SAMPLE):
    if len(ids) <= cap:
        return list(ids)
    return [ids[round(i*(len(ids)-1)/(cap-1))] for i in range(cap)]


def _audio_pair(a, b):
    scores = []
    for kind, weight in (("musicnn", .7), ("clap", .3)):
        similarity = cosine(a.get(kind), b.get(kind))
        if similarity is not None:
            scores.append(((1+similarity)/2, weight))
    return sum(x*w for x,w in scores)/sum(w for _,w in scores) if scores else None


def _fraction_valid(ids, ff, predicate):
    return sum(bool(predicate(ff.get(k, {}))) for k in ids)


def _metric(value, count, total, *, sample=None, required=MIN_AUDIO, threshold=MIN_COVERAGE):
    coverage = round(count/total, 4) if total else 0
    trustworthy = value is not None and count >= required and coverage >= threshold
    return {"score":round(clamp(value),4) if trustworthy else None,
            "observed":count, "total":total, "coverage":coverage,
            "sampled":sample,
            "status":"scored" if trustworthy else "insufficient"}


def _diversity(ids, lib, variation):
    n=len(ids)
    if not n:return None,{}
    artist=Counter(lib[k]["artist"] for k in ids)
    album=Counter(lib[k]["album_key"] for k in ids)
    art=1-sum((v/n)**2 for v in artist.values())
    alb=1-sum((v/n)**2 for v in album.values())
    actual=.65*art+.35*alb
    # A focused single-artist playlist is allowed; it is not automatically
    # harmed by low diversity. Variety preference remains a soft target.
    target={"focused":.24,"balanced":.56,"eclectic":.77}[variation]
    desirability=clamp(1-abs(actual-target)/.75)
    return desirability, {"artist_simpson":round(art,4),
                          "album_simpson":round(alb,4),
                          "variety_index":round(actual,4)}


def _energy(ids,ff,intent,reference):
    pairs=[ff[k]["energy"] for k in ids
           if k in ff and ff[k].get("energy") is not None]
    if not pairs:return None,0,{}
    mean=_mean(pairs)
    desired=ENERGY_LEVELS.get(intent["energy"],reference)
    score=clamp(1-abs(mean-desired)/.60) if desired is not None else None
    return score,len(pairs),{"mean_energy":round(mean,4),"target":round(desired,4) if desired is not None else None}


def _flow(ids,ff,intent,baseline):
    if len(ids)<2:return None,0,{}
    adjacent=[]
    jumps=[]
    for left,right in zip(ids,ids[1:]):
        a,b=ff.get(left,{}),ff.get(right,{})
        pair=_audio_pair(a,b)
        if pair is not None:adjacent.append(pair)
        if a.get("energy") is not None and b.get("energy") is not None:
            jumps.append(abs(a["energy"]-b["energy"]))
    if intent["flow"]=="smooth":
        if not adjacent and not jumps:return None,0,{}
        # Penalize abrupt transitions, not intentional moderate changes.
        sim=_mean(adjacent)
        energy=_mean(jumps)
        score=_mean([x for x in (
            sim,1-energy if energy is not None else None) if x is not None])
        count=min(len(ids),max(len(adjacent),len(jumps))+1)
    elif intent["flow"]=="varied":
        if not adjacent and not jumps:return None,0,{}
        sim=_mean(adjacent)
        energy=_mean(jumps)
        # Preferred variety involves transitions without losing all continuity.
        score=_mean([x for x in (
            clamp(1-abs(sim-.72)/.55) if sim is not None else None,
            clamp(1-abs(energy-.26)/.60) if energy is not None else None)
            if x is not None])
        count=min(len(ids),max(len(adjacent),len(jumps))+1)
    else:
        positions=[(i,ff[k]["energy"]) for i,k in enumerate(ids)
                   if k in ff and ff[k].get("energy") is not None]
        if not positions:return None,0,{}
        start=baseline if baseline is not None else _mean([v for _,v in positions])
        errors=[abs(value-clamp(start-.10+.24*math.sin(math.pi*i/max(1,len(ids)-1))))
                for i,value in positions]
        score=clamp(1-_mean(errors)/.6)
        count=len(positions)
    return score,count,{"mean_neighbor_similarity":round(_mean(adjacent),4) if adjacent else None,
                       "mean_energy_jump":round(_mean(jumps),4) if jumps else None}


def _intent(ids,lib,ff,intent,baseline):
    from .playlist_ranking import _intent_score
    scores=[]
    for key in ids:
        value,_=_intent_score(ff.get(key,{}),lib[key],intent,baseline)
        if value is not None:scores.append(value)
    return _mean(scores),len(scores)


def measure(ids, library, ff, identity, *, reference=None, pair_cache=None):
    """Return all diagnostic dimensions and honest per-dimension coverage."""
    keys=list(ids)
    if any(k not in library for k in keys):
        raise ValueError("Playlist references tracks missing from the Plex catalogue.")
    n=len(keys)
    audioids=[k for k in keys if k in ff and (
        ff[k].get("musicnn") is not None or ff[k].get("clap") is not None)]
    sample=_sample(audioids)
    pair_scores=[]
    pair_cache = pair_cache if pair_cache is not None else {}
    for i,key in enumerate(sample):
        current=[]
        for other in sample:
            if other==key:continue
            pair=tuple(sorted((key,other)))
            if pair not in pair_cache:
                pair_cache[pair]=_audio_pair(ff[key],ff[other])
            sc=pair_cache[pair]
            if sc is not None:current.append(sc)
        current.sort(reverse=True)
        if current:pair_scores.append(_mean(current[:min(3,len(current))]))
    coherence=_metric(_mean(pair_scores),len(audioids),n,
                      sample=len(sample),required=MIN_AUDIO)
    diversity,details=_diversity(keys,library,identity["variation"])
    variation=_metric(diversity,n,n,required=1,threshold=1)
    variation["detail"]=details
    observed_energy=[ff[k]["energy"] for k in keys if k in ff and ff[k].get("energy") is not None]
    target=reference if reference is not None else _mean(observed_energy)
    score,energy_n,extra=_energy(keys,ff,identity,target)
    energy=_metric(score,energy_n,n)
    energy["detail"]=extra
    flow_score,flow_n,flow_details=_flow(keys,ff,identity,target)
    flow=_metric(flow_score,flow_n,n)
    flow["detail"]=flow_details
    intent_score,intent_n=_intent(keys,library,ff,identity,target)
    alignment=_metric(intent_score,intent_n,n)
    return {"coherence":coherence,"variation":variation,
            "energy":energy,"flow":flow,"intent":alignment,
            "counts":{"tracks":n,"audio_analyzed":len(audioids),
                      "energy_analyzed":energy_n,"intent_analyzed":intent_n}}


def compare(before, after, library, cache, identity, *, decoded=False,
            pair_cache=None, baseline=None):
    """Compare the same metrics using a fixed energy reference from BEFORE.

    Paired scores do not depend on proposal order; a no-op has exact zero
    deltas. Absolute metric availability and missing data stay visible.
    """
    ff=cache if decoded else features_for(cache)
    energy_values=[ff[k]["energy"] for k in before if k in ff and
                   ff[k].get("energy") is not None]
    reference=_mean(energy_values)
    pair_cache = pair_cache if pair_cache is not None else {}
    a=baseline if baseline is not None else measure(
        before,library,ff,identity,reference=reference,pair_cache=pair_cache)
    b=measure(after,library,ff,identity,reference=reference,pair_cache=pair_cache)
    changes={}
    for metric in NAMES:
        old,new=a[metric],b[metric]
        valid=old["score"] is not None and new["score"] is not None
        delta=round(new["score"]-old["score"],4) if valid else None
        reasons=[]
        if not valid:
            reasons.append("Insufficient observed data for paired comparison")
        if old["coverage"] < 1 or new["coverage"] < 1:
            reasons.append("Incomplete feature coverage")
        if old.get("sampled") is not None and (
            old["sampled"] < old["observed"] or new["sampled"] < new["observed"]):
            reasons.append("Coherence based on stratified subsample")
        if metric=="variation":reasons.append("Target diversity is a configurable heuristic")
        changes[metric]={"before":old,"after":new,"delta":delta,
                         "direction":"improved" if delta is not None and delta>.005
                         else "worse" if delta is not None and delta<-.005
                         else "unchanged" if delta is not None else "unknown",
                         "uncertainty":reasons}
    valid=[d["delta"] for d in changes.values() if d["delta"] is not None]
    return {"metrics":changes,"comparable":len(valid)==len(NAMES),
            "scored_dimensions":len(valid), "total_dimensions":len(NAMES),
            "basis":"heuristic_audio_and_library_metadata",
            "warning":"Exploratory indicators, not measured listening quality; review by ear.",
            "before_tracks":len(before),"after_tracks":len(after)}
