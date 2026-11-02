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


def calibration(playlist, variation, *, current_identity_revision=None):
    """Fit modest bounded factor weights from distinct human positive/negative labels.

    Minimum 2 positive and 2 negative reviews with comparable feature data.
    Old identity revisions remain audit evidence but cannot tune a new profile.
    """
    baseline=SCORE_WEIGHTS[variation]
    rows=[r for r in reviews(playlist) if r["listened"] and
          (current_identity_revision is None or
           r["identity_revision"]==current_identity_revision)]
    positive=[r for r in rows if r["verdict"]=="accept"]
    negative=[r for r in rows if r["verdict"]=="reject"]
    enough=len(positive)>=2 and len(negative)>=2
    weights=dict(baseline)
    evidence={}
    if enough:
        for factor in FACTOR_KEYS:
            pos=[r["factors"][factor] for r in positive if factor in r["factors"]]
            neg=[r["factors"][factor] for r in negative if factor in r["factors"]]
            if len(pos)<2 or len(neg)<2:continue
            difference=sum(pos)/len(pos)-sum(neg)/len(neg)
            # Restrict update to ±25%; avoid an overfit collapse of a factor.
            weights[factor]=baseline[factor]*max(.75,min(1.25,1+0.5*difference))
            evidence[factor]=round(difference,4)
        normalizer=sum(weights.values())
        weights={key:value/normalizer for key,value in weights.items()}
    return {"weights":weights,"trained":enough and bool(evidence),
            "reviewed":len(rows),"accepted":len(positive),"rejected":len(negative),
            "factor_separation":evidence,
            "warning":"Small-sample per-playlist preference calibration; human labels, not proof of quality."}


def feedback_for(playlist, identity_revision):
    return {(r["target_id"],r["candidate_id"]):r for r in reviews(playlist)
            if r["identity_revision"]==identity_revision}


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


def summary(playlists):
    """Validation coverage across actual playlist names, not synthetic fixtures."""
    result={}
    for name in playlists:
        records=reviews(name)
        a=sum(r["verdict"]=="accept" for r in records)
        b=sum(r["verdict"]=="reject" for r in records)
        issues=dict(Counter(r["issue"] for r in records if r["verdict"]=="reject"))
        result[name]={"reviewed":len(records),"accepted":a,"rejected":b,
                      "issue_counts":issues,"human_validated":len(records)>0,
                      "acceptance_rate":round(a/len(records),3) if records else None}
    return result
