"""Migration keeps all upstream analysis fields without deleting legacy results."""
from contextlib import closing

from app import audiomuse_client as ac


def test_full_raw_audio_sync_backfills_legacy_cache_in_bounded_batches(tmp_path,monkeypatch):
    monkeypatch.setattr(ac,"CACHE_FILE",tmp_path/"audiomuse.sqlite")
    monkeypatch.setattr(ac,"_FEATURES_HOT",{})
    monkeypatch.setattr(ac,"MAX_PER_REQUEST",1)
    with closing(ac._open_cache()) as conn:
        with conn:
            conn.execute("INSERT INTO audio_features(track_id,fp,data) VALUES(?,?,?)",
                         ("1","same-fingerprint",'{"energy":0.5}'))
    def upstream(*args,**kwargs):
        return {"1":"same-fingerprint","2":"second"}
    monkeypatch.setattr(ac,"_settings",lambda:("http://audio.invalid","secret"))
    monkeypatch.setattr(ac,"_get_index",upstream)
    fetched=[]
    def get(url,token,params):
        fetched.append(params["ids"])
        ids=params["ids"].split(",")
        return {"tracks":[{"id":key,"fp":upstream()[key],"energy":0.5,
                           "future_measurement":{"spectral": [0.2,0.9]},
                           "embedding":None,"clap_embedding":None} for key in ids]}
    monkeypatch.setattr(ac,"_get",get)
    first=ac.synchronize()
    assert first["new_or_updated"]==1 and first["pending"]==1
    with closing(ac._open_cache()) as conn:
        versions=dict(conn.execute("SELECT track_id,raw_version FROM audio_features"))
    assert versions["1"]==2
    assert "2" not in versions
    second=ac.synchronize()
    assert second["new_or_updated"]==1 and second["pending"]==0
    again=ac.synchronize()
    assert again["new_or_updated"]==0
    assert fetched==["1","2"]
    stored,state=ac.cached_features()
    assert stored["1"]["id"]=="1"
    assert stored["1"]["fp"]=="same-fingerprint"
    assert stored["1"]["future_measurement"]=={"spectral":[0.2,0.9]}
    assert stored["2"]["future_measurement"]["spectral"]==[0.2,0.9]
    with closing(ac._open_cache()) as conn:
        versions=dict(conn.execute("SELECT track_id,raw_version FROM audio_features"))
    assert versions=={"1":2,"2":2}
