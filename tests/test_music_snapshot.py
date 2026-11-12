"""Disk/Plex inventory is acquired only once across rapid filter changes."""
from types import SimpleNamespace as NS
from app import music_snapshot as snapshot


def test_repeated_reads_share_one_complete_snapshot_and_explicit_invalidation():
    snapshot.invalidate()
    calls={"disk":0,"plex":0}
    def inventory():
        calls["disk"]+=1
        return {"albums":["Album"]}
    def connect():
        calls["plex"]+=1
        return object(),NS(searchTracks=lambda:[NS(title="Track")],
                           locations=["/music"])
    one=snapshot.get(inventory,connect)
    two=snapshot.get(inventory,connect)
    assert one["cached"] is False and two["cached"] is True
    assert calls=={"disk":1,"plex":1}
    assert one["inventory"]==two["inventory"]
    snapshot.invalidate()
    next=snapshot.get(inventory,connect)
    assert next["cached"] is False
    assert calls=={"disk":2,"plex":2}


def test_cache_age_expires_without_new_user_interaction(monkeypatch):
    snapshot.invalidate()
    now=[100.]
    monkeypatch.setattr(snapshot.time,"monotonic",lambda:now[0])
    calls=[]
    def inventory():calls.append(1);return {}
    def connect():return None,NS(searchTracks=lambda:[],locations=[])
    snapshot.get(inventory,connect)
    now[0]+=snapshot.TTL_SECONDS+1
    assert snapshot.get(inventory,connect)["cached"] is False
    assert len(calls)==2
