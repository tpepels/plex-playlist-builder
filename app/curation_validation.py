"""Durable listening validation and conservative ranking recalibration.

A human review requires an explicit listening attestation. Observed labels are
not claimed as a scientific generalization: feedback tunes only the particular
playlist and retains its explanation. This module never touches Plex playlists.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading

from .playlist_ranking import SCORE_WEIGHTS
from .insights import normalize

FILE=Path(os.getenv("CURATION_VALIDATION_FILE","/data/curation-validation.sqlite"))
LOCK=threading.RLock()
VERDICTS={"accept","reject"}
ISSUES={"none","genre","energy","mood","flow","variety","recording","metadata","other"}
FACTOR_KEYS=("fit","diversity","flow","intent")


def _db():
    FILE.parent.mkdir(parents=True,exist_ok=True)
    connection=sqlite3.connect(FILE, timeout=15)
    connection.execute("""CREATE TABLE IF NOT EXISTS reviews (
      playlist TEXT NOT NULL, target_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
      verdict TEXT NOT NULL, issue TEXT NOT NULL, note TEXT NOT NULL,
      identity_revision TEXT NOT NULL, playlist_snapshot TEXT NOT NULL,
      original_score REAL, factors TEXT NOT NULL, listened INTEGER NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (playlist,target_id,candidate_id)
    )""")
    connection.commit()
    return connection


def _row(row):
    return {"playlist":row[0],"target_id":row[1] or None,"candidate_id":row[2],
            "verdict":row[3],"issue":row[4],"note":row[5],
            "identity_revision":row[6],"playlist_snapshot":row[7],
            "original_score":row[8],"factors":json.loads(row[9]),
            "listened":bool(row[10]),"updated_at":row[11]}


def reviews(playlist):
    # Reading an uninitialized validator must be side-effect-free, including
    # on fresh installations and CI hosts without a writable /data mount.
    if not FILE.is_file():
        return []
    with LOCK, _db() as db:
        return [_row(row) for row in db.execute(
            "SELECT playlist,target_id,candidate_id,verdict,issue,note,"
            "identity_revision,playlist_snapshot,original_score,factors,listened,updated_at "
            "FROM reviews WHERE playlist=? ORDER BY updated_at DESC", (playlist,))]


def record(playlist, target, candidate, verdict, issue, note, listened,
           identity_revision, playlist_snapshot, original_score, factors):
    if verdict not in VERDICTS or issue not in ISSUES:
        raise ValueError("Invalid validation outcome or issue.")
    if verdict=="accept" and issue!="none":
        raise ValueError("Accepted suggestions must use issue 'none'.")
    if verdict=="reject" and issue=="none":
        raise ValueError("Specify why the suggestion was rejected.")
    if listened is not True:
        raise ValueError("Listening confirmation is required for musical validation.")
    if not isinstance(note,str) or len(note)>500:
        raise ValueError("Validation note must be at most 500 characters.")
    if not isinstance(factors,dict) or set(factors)-set(FACTOR_KEYS):
        raise ValueError("Invalid component score snapshot.")
    clean={}
    for k,v in factors.items():
        if v is None:continue
        if not isinstance(v,(float,int)) or not 0<=v<=1:
            raise ValueError("Invalid component score.")
        clean[k]=float(v)
    if original_score is not None and (not isinstance(original_score,(int,float))
                                       or not 0<=original_score<=1):
        raise ValueError("Invalid original candidate score.")
    if not playlist or not candidate or not isinstance(identity_revision,str):
        raise ValueError("Missing review identity.")
    with LOCK, _db() as db:
        db.execute("""INSERT INTO reviews
            (playlist,target_id,candidate_id,verdict,issue,note,identity_revision,
             playlist_snapshot,original_score,factors,listened,updated_at)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
             ON CONFLICT(playlist,target_id,candidate_id) DO UPDATE SET
             verdict=excluded.verdict,issue=excluded.issue,note=excluded.note,
             identity_revision=excluded.identity_revision,
             playlist_snapshot=excluded.playlist_snapshot,
             original_score=excluded.original_score,factors=excluded.factors,
             listened=excluded.listened,updated_at=excluded.updated_at""",
          (playlist,target or "",str(candidate),verdict,issue,note.strip(),
           identity_revision,playlist_snapshot,original_score,
           json.dumps(clean,sort_keys=True),1,
           datetime.now(timezone.utc).isoformat(timespec="seconds")))
        db.commit()
    return {"saved":True,"playlist":playlist,
            "target_id":target or None,"candidate_id":str(candidate),
            "verdict":verdict}


def _score(factors,weights):
    pairs=[(key,value) for key,value in factors.items()
           if key in weights and value is not None]
    return (sum(value*weights[key] for key,value in pairs) /
            sum(weights[key] for key,_ in pairs)) if pairs else None


def _auc(points):
    positive=[s for s,y in points if s is not None and y]
    negative=[s for s,y in points if s is not None and not y]
    if not positive or not negative:return None
    return round(sum(1 if p>n else .5 if p==n else 0
                     for p in positive for n in negative) /
                 (len(positive)*len(negative)),4)


def _fit_weights(rows,variation):
    baseline=SCORE_WEIGHTS[variation]
    accepted=[r for r in rows if r["verdict"]=="accept"]
    rejected=[r for r in rows if r["verdict"]=="reject"]
    if len(accepted)<2 or len(rejected)<2:
        return dict(baseline),{}
    weights=dict(baseline)
    evidence={}
    for factor in FACTOR_KEYS:
        pos=[r["factors"][factor] for r in accepted if factor in r["factors"]]
        neg=[r["factors"][factor] for r in rejected if factor in r["factors"]]
        if len(pos)<2 or len(neg)<2:continue
        difference=sum(pos)/len(pos)-sum(neg)/len(neg)
        weights[factor]=baseline[factor]*max(.75,min(1.25,1+.5*difference))
        evidence[factor]=round(difference,4)
    total=sum(weights.values())
    return {key:value/total for key,value in weights.items()},evidence


def calibration(playlist,variation,*,current_identity_revision=None,
                current_playlist_snapshot=None):
    """Bounded feedback adaptation, gated on leave-one-out discrimination."""
    baseline=dict(SCORE_WEIGHTS[variation])
    rows=[r for r in reviews(playlist) if r["listened"] and
          (current_identity_revision is None or
           r["identity_revision"]==current_identity_revision) and
          (current_playlist_snapshot is None or
           r["playlist_snapshot"]==current_playlist_snapshot)]
    pos=sum(r["verdict"]=="accept" for r in rows)
    neg=len(rows)-pos
    fitted,evidence=_fit_weights(rows,variation)
    old_auc=_auc([(_score(r["factors"],baseline),
                   r["verdict"]=="accept") for r in rows])
    enough=len(rows)>=8 and pos>=3 and neg>=3 and bool(evidence)
    held_out_auc=None
    trained=False
    if enough:
        held_out=[]
        for i,row in enumerate(rows):
            weights,_=_fit_weights(rows[:i]+rows[i+1:],variation)
            held_out.append((_score(row["factors"],weights),
                             row["verdict"]=="accept"))
        held_out_auc=_auc(held_out)
        trained=(held_out_auc is not None and old_auc is not None
                 and held_out_auc>=old_auc+.02 and held_out_auc>=.55)
    return {"weights":fitted if trained else baseline,
            "trained":trained,
            "status":"held_out_improvement" if trained else
                     "insufficient_reviews" if not enough else "no_held_out_gain",
            "reviewed":len(rows),"accepted":pos,"rejected":neg,
            "factor_separation":evidence,
            "baseline_auc":old_auc,"held_out_auc":held_out_auc,
            "warning":"Small-sample human preference evidence; no automatic weight change without out-of-sample improvement."}


def _wilson(accepted,total,z=1.96):
    if not total:return None
    p=accepted/total
    scale=1+z*z/total
    middle=(p+z*z/(2*total))/scale
    half=z*((p*(1-p)+z*z/(4*total))/total)**.5/scale
    return [round(max(0,middle-half),3),round(min(1,middle+half),3)]


def feedback_for(playlist, identity_revision,playlist_snapshot=None):
    return {(r["target_id"],r["candidate_id"]):r for r in reviews(playlist)
            if r["identity_revision"]==identity_revision and
               (playlist_snapshot is None or
                r["playlist_snapshot"]==playlist_snapshot)}


def categories(names, descriptions=None, manual_styles=None):
    """Choose existing, not fabricated, examples across four musical families."""
    descriptions=descriptions or {}
    manual_styles=manual_styles or {}
    pools={"jazz":[],"ambient":[],"soul":[],"mixed":[]}
    for name in sorted(names):
        text=normalize(name+" "+str(descriptions.get(name,"")))
        styles={normalize(x) for x in manual_styles.get(name,[])}
        words=set(text.split())
        if "jazz" in words or "jazz" in styles:pools["jazz"].append(name)
        if "ambient" in words or "ambient" in styles:pools["ambient"].append(name)
        if "soul" in words or "soul" in styles:pools["soul"].append(name)
        mixed=("mix" in words or "mixed" in words or "fusion" in words
               or "eclectic" in words or "same room" in text
               or len(styles)>=2
               or sum(x in words for x in ("jazz","soul","ambient","beats","classical"))>=2)
        if mixed:pools["mixed"].append(name)
    selected={}
    used=set()
    for cat in ("mixed","ambient","soul","jazz"):
        choice=next((x for x in pools[cat] if x not in used),None)
        selected[cat]=choice
        if choice:used.add(choice)
    return selected


def portfolio(names,descriptions=None,manual_styles=None,per_category=2):
    """Select diverse existing cases per family, with no invented playlists."""
    if not 1<=per_category<=5:raise ValueError("per_category must be 1–5")
    descriptions=descriptions or {}
    manual_styles=manual_styles or {}
    pools={"jazz":[],"ambient":[],"soul":[],"mixed":[]}
    for name in sorted(names):
        source=normalize(name+" "+str(descriptions.get(name,"")))
        words=set(source.split())
        styles=set(normalize(x) for x in manual_styles.get(name,[]))
        if "jazz" in words or "jazz" in styles:pools["jazz"].append(name)
        if "ambient" in words or "ambient" in styles:pools["ambient"].append(name)
        if "soul" in words or "soul" in styles:pools["soul"].append(name)
        if ("mix" in words or "mixed" in words or "fusion" in words
                or "eclectic" in words or "same room" in source
                or len(styles)>=2 or sum(x in words for x in
                ("jazz","soul","ambient","beats","classical"))>=2):
            pools["mixed"].append(name)
    result={kind:[] for kind in pools}
    used=set()
    for kind in ("mixed","ambient","soul","jazz"):
        for _ in range(per_category):
            available=[name for name in pools[kind] if name not in used]
            if not available:break
            selected=result[kind]
            if selected:
                def overlap(name):
                    tokens=set(normalize(name).split())
                    return max(len(tokens & set(normalize(prev).split())) /
                               max(1,len(tokens | set(normalize(prev).split())))
                               for prev in selected)
                # Prefer a different context instead of two near-duplicate titles.
                choice=min(available,key=lambda name:(overlap(name),name))
            else:
                choice=available[0]
            used.add(choice)
            selected.append(choice)
    return result


def summary(playlists,active_versions=None):
    """Report CURRENT reviews, withholding stale playlist/identity judgments."""
    result={}
    for name in playlists:
        records=reviews(name)
        historical=len(records)
        if active_versions is not None:
            current=active_versions.get(name)
            records=[r for r in records if current is not None and
                     r["playlist_snapshot"]==current[0] and
                     r["identity_revision"]==current[1]]
        a=sum(r["verdict"]=="accept" for r in records)
        b=sum(r["verdict"]=="reject" for r in records)
        issues=dict(Counter(r["issue"] for r in records if r["verdict"]=="reject"))
        result[name]={"reviewed":len(records),"reviewed_historical":historical,
                      "accepted":a,"rejected":b,
                      "issue_counts":issues,"human_validated":len(records)>=8,
                      "acceptance_rate":round(a/len(records),3) if records else None,
                      "acceptance_interval_95":_wilson(a,len(records)),
                      "distinct_targets":len({r["target_id"] for r in records})}
    return result
