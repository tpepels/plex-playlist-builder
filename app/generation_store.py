"""Persistent reviewable playlist-generation drafts with optimistic revisions.

A draft is never published automatically; a separate confirm operation checks
the exact revision, YAML fingerprint, track list and playlist name.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
from datetime import datetime, timezone

FILE=Path(os.getenv("PLAYLIST_GENERATION_DB","/data/playlist-generations.sqlite"))
LOCK=threading.RLock()


class DraftConflict(ValueError):
    pass


def _connect():
    FILE.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(FILE,timeout=15)
    db.execute("""CREATE TABLE IF NOT EXISTS generation_drafts (
       id TEXT PRIMARY KEY,revision INTEGER NOT NULL,
       status TEXT NOT NULL,data TEXT NOT NULL,
       published_playlist_id TEXT,updated_at TEXT NOT NULL
    )""")
    db.commit()
    return db


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _decode(row):
    if row is None:raise DraftConflict("Unknown playlist-generation draft.")
    return {"id":row[0],"revision":row[1],"status":row[2],
            "data":json.loads(row[3]),"playlist_id":row[4],"updated_at":row[5]}


def create(data):
    with LOCK, _connect() as db:
        identifier=secrets.token_urlsafe(18)
        db.execute("INSERT INTO generation_drafts VALUES(?,?,?,?,?,?)",
                   (identifier,1,"draft",json.dumps(data,ensure_ascii=False),
                    None,_now()))
        db.commit()
        return get(identifier)


def get(identifier):
    if not isinstance(identifier,str) or len(identifier)>128:
        raise DraftConflict("Invalid draft ID.")
    with LOCK, _connect() as db:
        row=db.execute("""SELECT id,revision,status,data,published_playlist_id,updated_at
                          FROM generation_drafts WHERE id=?""",(identifier,)).fetchone()
        return _decode(row)


def revise(identifier,expected_revision,data):
    with LOCK, _connect() as db:
        cursor=db.execute("""UPDATE generation_drafts
          SET revision=revision+1,data=?,updated_at=?
          WHERE id=? AND revision=? AND status='draft'""",
          (json.dumps(data,ensure_ascii=False),_now(),identifier,expected_revision))
        if cursor.rowcount!=1:
            raise DraftConflict("Draft was changed or already published. Reload.")
        db.commit()
        return get(identifier)


def mark_published(identifier,expected_revision,playlist_id,title=None):
    with LOCK, _connect() as db:
        row=db.execute("""SELECT data FROM generation_drafts
            WHERE id=? AND revision=? AND status='draft'""",
            (identifier,expected_revision)).fetchone()
        if row is None:
            raise DraftConflict("Draft was changed while publishing.")
        payload=json.loads(row[0])
        if title is not None:payload["published_name"]=title
        cursor=db.execute("""UPDATE generation_drafts
          SET status='published',published_playlist_id=?,data=?,updated_at=?
          WHERE id=? AND revision=? AND status='draft'""",
          (str(playlist_id),json.dumps(payload,ensure_ascii=False),_now(),
           identifier,expected_revision))
        if cursor.rowcount!=1:
            raise DraftConflict("Draft was changed while publishing.")
        db.commit()
        return get(identifier)
