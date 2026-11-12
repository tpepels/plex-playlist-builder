"""Complete collaboration archive and in-place, approval-gated proposal round trip."""
import base64
import csv
from io import StringIO
import io
import json
import struct
import zipfile
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from app import collaboration as c
from app import config_store, generation_store, main
from app.playlist_generation import catalogue_revision


def catalogue():
    return {str(i):{
        "key":str(i),"album_key":str(i//2+1),"artist":"Artist "+str(i),
        "album":"Album","title":"Track "+str(i),"disc":1,"index":i,
        "genres":["Jazz","Hard bop"],"year":1960+i,
        "duration_ms":200000+i*1000,
    } for i in range(1,7)}


def audio():
    a=base64.b64encode(struct.pack("<4f",.25,.5,-.75,1)).decode()
    b=base64.b64encode(struct.pack("<4f",.2,.3,.4,.5)).decode()
    return {
        "1":{"artist":"Audio Artist","tempo":124,"energy":.6,
             "key":"Eb","scale":"major",
             "other_features":{"danceable":.8,"custom_measurement":.123},
             "mood_vector":[.1,.9],"embedding":a,"clap_embedding":b,
             "unrecognized_future_field":{"foo":"bar"}},
        "2":{"tempo":80,"embedding":a,"clap_embedding":None},
        "3":{"tempo":0}, # measured but no embeddings
        "6":{"embedding":"BAD--","clap_embedding":b},
        "999":{"energy":.9,"embedding":b}, # orphan kept losslessly
    }


class Playlist:
    def __init__(self, plex, title, ids, rating_key):
        self.plex=plex
        self.title=title
        self.ratingKey=rating_key
        self._ids=list(ids)
        self.smart=False
        self.radio=False
        self.deleted=False
    def items(self):
        return [SimpleNamespace(ratingKey=int(i)) for i in self._ids]
    def addItems(self, items):
        self._ids.extend(str(x.ratingKey) for x in items)
    def removeItems(self, track):
        self._ids.remove(str(track.ratingKey))
    def moveItem(self, track, after=None):
        key=str(track.ratingKey)
        self._ids.remove(key)
        if after is None:self._ids.insert(0,key)
        else:self._ids.insert(self._ids.index(str(after.ratingKey))+1,key)
    def delete(self):
        self.deleted=True
        self.plex._playlists.remove(self)


class Plex:
    def __init__(self):
        self._playlists=[]
        self.creates=0
    def playlists(self, playlistType="audio"):
        assert playlistType=="audio"
        return list(self._playlists)
    def fetchItem(self, key):
        assert str(key) in catalogue()
        return SimpleNamespace(TYPE="track", ratingKey=int(key))
    def createPlaylist(self,title,items):
        self.creates+=1
        p=Playlist(self,title,[str(x.ratingKey) for x in items],900+self.creates)
        self._playlists.append(p)
        return p


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store,"PLAYLISTS_FILE",tmp_path/"playlists.yml")
    monkeypatch.setattr(config_store,"STATE_FILE",tmp_path/"playlist-state.yml")
    monkeypatch.setattr(generation_store,"FILE",tmp_path/"drafts.sqlite")
    playlist={"description":"Preserved original","enabled":True,"strict":True,
              "items":[{"rating_key":x} for x in ("1","2","3")]}
    config_store.save_text(yaml.safe_dump(
        {"playlists":{"Existing":playlist}},sort_keys=False))
    p=Plex()
    existing=Playlist(p,"Existing",["1","2","3"],610)
    p._playlists.append(existing)
    return p,existing,catalogue(),audio()


def proposal(library,defs,items):
    return {"format":c.PROPOSAL_FORMAT,
            "source":{"library_revision":catalogue_revision(library),
                      "playlist_revision":config_store.fingerprint(defs)},
            "playlists":items}


def new(name="Fresh jazz",keys=None):
    return {"name":name,"action":"create","rating_keys":keys or ["4","5"],
            "description":"Collaboratively selected hard bop"}


def update(keys=None):
    return {"name":"Existing","action":"update","rating_keys":keys or ["3","2","4"],
            "notes":"Smoother energy curve"}


def test_full_export_preserves_every_raw_audio_field_and_full_embeddings(env):
    plex,existing,lib,features=env
    path, manifest=c.build_export_zip(lib,features,config_store.load()["playlists"],
                                      catalogue_updated=123,cache_state={
                                          "last_sync":"2026-11-12T01:00:00",
                                          "manifest_version":"AudioMuse 4"})
    try:
        with zipfile.ZipFile(path) as archive:
            assert archive.namelist()==[
                "tracks.csv","embeddings.jsonl","playlists.yml","manifest.json"]
            rows=list(csv.DictReader(StringIO(archive.read("tracks.csv").decode())))
            emb=[json.loads(line) for line in archive.read("embeddings.jsonl").decode().splitlines()]
            embedded={x["rating_key"]:x for x in emb}
            assert len(rows)==6 and len(emb)==7
            assert {r["rating_key"] for r in rows}==set(lib)
            assert rows[0]["analysis_status"]=="complete_embeddings"
            assert rows[0]["tempo"]=="124"
            assert rows[0]["genres_json"]=='["Jazz","Hard bop"]'
            assert json.loads(rows[0]["audio_analysis_raw_json"])["unrecognized_future_field"]=={"foo":"bar"}
            assert next(r for r in rows if r["rating_key"]=="4")["analysis_status"]=="missing_analysis"
            assert next(r for r in rows if r["rating_key"]=="2")["analysis_status"]=="partial_embeddings"
            assert next(r for r in rows if r["rating_key"]=="3")["analysis_status"]=="no_embeddings"
            assert next(r for r in rows if r["rating_key"]=="6")["analysis_status"]=="partial_embeddings"
            assert embedded["1"]["musicnn_embedding"]==[.25,.5,-.75,1]
            assert embedded["1"]["clap_embedding"]==pytest.approx([.2,.3,.4,.5])
            assert embedded["1"]["audio_analysis_raw"]["unrecognized_future_field"]=={"foo":"bar"}
            assert embedded["1"]["musicnn_base64"]==features["1"]["embedding"]
            assert embedded["4"]["audio_analysis_raw"] is None
            assert embedded["6"]["musicnn_status"]=="invalid"
            assert embedded["999"]["in_plex_library"] is False
            assert embedded["999"]["audio_analysis_raw"]==features["999"]
            yaml_data=yaml.safe_load(archive.read("playlists.yml"))
            assert yaml_data["playlists"]["Existing"]["description"]=="Preserved original"
            assert [i["rating_key"] for i in yaml_data["playlists"]["Existing"]["items"]]==["1","2","3"]
            saved_manifest=json.loads(archive.read("manifest.json"))
            assert saved_manifest["library_revision"]==catalogue_revision(lib)
            assert saved_manifest["playlist_revision"]==config_store.fingerprint(yaml_data["playlists"])
            assert saved_manifest["counts"]["missing_analysis"]==2
            assert saved_manifest["counts"]["orphan_audio_records"]==1
            assert saved_manifest["counts"]["complete_embeddings"]==1
            assert saved_manifest["audio_last_sync"]=="2026-11-12T01:00:00"
            assert "api_token" not in str(saved_manifest).lower()
    finally:
        path.unlink(missing_ok=True)


def test_empty_library_export_refuses_partial_snapshot(env):
    with pytest.raises(c.CollaborationError,match="complete library"):
        c.build_export_zip({}, {}, {})


def test_proposal_preview_exact_order_is_read_only(env):
    plex,original,lib,features=env
    definition=config_store.load()["playlists"]
    requested=proposal(lib,definition,[update(),new()])
    before=config_store.read_text()
    preview=c.preview_proposal(requested,lib,definition,plex)
    assert preview["changes"]==2
    assert preview["previews"][0]["playlist_id"]=="610"
    assert preview["previews"][0]["before_count"]==3
    assert preview["previews"][0]["after_count"]==3
    assert preview["previews"][0]["added"]==1
    assert preview["previews"][0]["removed"]==1
    assert [x["id"] for x in preview["previews"][0]["track_order"]]==["3","2","4"]
    assert preview["untouched_playlists"]==0
    assert config_store.read_text()==before
    assert original._ids==["1","2","3"]
    assert plex.creates==0


def test_approval_uses_in_place_update_and_existing_new_playlist_publication(env):
    plex,original,lib,_=env
    entry=proposal(lib,config_store.load()["playlists"],[update(),new()])
    result=c.preview_proposal(entry,lib,config_store.load()["playlists"],plex)
    with pytest.raises(c.CollaborationError,match="Explicit"):
        c.apply_proposal(entry,result["review_token"],lib,plex,approved=False)
    applied=c.apply_proposal(entry,result["review_token"],lib,plex,approved=True)
    assert applied["ok"] is True, applied
    assert [(r["name"],r["status"]) for r in applied["results"]]==[
        ("Existing","synced"),("Fresh jazz","published")]
    assert plex.creates==1
    assert plex._playlists[0] is original
    assert original.ratingKey==610 and not original.deleted
    assert original._ids==["3","2","4"]
    assert plex._playlists[1]._ids==["4","5"]
    stored=config_store.load()["playlists"]
    assert stored["Existing"]["description"]=="Preserved original"
    assert [x["rating_key"] for x in stored["Existing"]["items"]]==["3","2","4"]
    assert stored["Fresh jazz"]["description"]=="Collaboratively selected hard bop"
    assert config_store.version_details(stored)["sync_statuses"]["Existing"]=="synced"
    assert config_store.version_details(stored)["sync_statuses"]["Fresh jazz"]=="synced"
    with pytest.raises(c.CollaborationError,match="changed|differs"):
        c.apply_proposal(entry,result["review_token"],lib,plex,approved=True)
    assert plex.creates==1


def test_outdated_source_or_live_order_rejects_before_any_write(env):
    plex,original,lib,_=env
    data=proposal(lib,config_store.load()["playlists"],[update()])
    reviewed=c.preview_proposal(data,lib,config_store.load()["playlists"],plex)
    original._ids=["2","1","3"]
    with pytest.raises(c.CollaborationError,match="changed after preview"):
        c.apply_proposal(data,reviewed["review_token"],lib,plex,approved=True)
    assert original._ids==["2","1","3"]
    original._ids=["1","2","3"]
    data["source"]["playlist_revision"]="0"*12
    with pytest.raises(c.CollaborationError,match="Saved playlists changed"):
        c.preview_proposal(data,lib,config_store.load()["playlists"],plex)


def test_invalid_ids_protected_names_and_description_changes_fail_closed(env):
    plex,original,lib,_=env
    defs=config_store.load()["playlists"]
    for item in [
        update(["3","9999"]),
        update(["1","1"]),
        new("Existing",["1"]),
        {"name":"All Music","action":"create","rating_keys":["4"]},
        {**update(),"description":"Unapproved change"},
        {"name":"Existing","action":"create","rating_keys":["4"]},
    ]:
        data=proposal(lib,defs,[item])
        with pytest.raises(c.CollaborationError):
            c.preview_proposal(data,lib,defs,plex)
    assert original._ids==["1","2","3"]
    assert len(plex._playlists)==1


def test_json_and_zip_proposals_decode_losslessly(env):
    plex,original,lib,_=env
    payload=proposal(lib,config_store.load()["playlists"],[new()])
    raw=json.dumps(payload).encode()
    assert c.decode_proposal(raw,"application/json")==c.normalize_proposal(payload)
    compressed=io.BytesIO()
    with zipfile.ZipFile(compressed,"w") as archive:
        archive.writestr("proposal.json",raw)
    assert c.decode_proposal(compressed.getvalue(),"application/zip")==c.normalize_proposal(payload)
    with pytest.raises(c.CollaborationError,match="proposal.json"):
        c.decode_proposal(b"PK\x03\x04INVALID","application/zip")
    with pytest.raises(c.CollaborationError):
        c.normalize_proposal({"format":"wrong","source":{},"playlists":[]})


def test_partial_application_reports_exact_failure_and_stops(env,monkeypatch):
    plex,original,lib,_=env
    entry=proposal(lib,config_store.load()["playlists"],[
        update(),new(),new("Last jazz",["5","6"])])
    reviewed=c.preview_proposal(entry,lib,config_store.load()["playlists"],plex)
    def broken(*args,**kwargs):
        raise RuntimeError("Simulated Plex loss")
    monkeypatch.setattr(plex,"createPlaylist",broken)
    result=c.apply_proposal(entry,reviewed["review_token"],lib,plex,approved=True)
    assert result["ok"] is False
    assert result["results"][0]["name"]=="Existing"
    assert result["failed"]["name"]=="Fresh jazz"
    assert "Last jazz" in result["not_attempted"]
    assert original._ids==["3","2","4"]
    assert not any(p.title=="Fresh jazz" for p in plex._playlists)


def test_api_zip_export_and_signed_proposal_preview_apply(env,monkeypatch):
    plex,original,lib,features=env
    monkeypatch.setattr(main,"cached_library",lambda:lib)
    monkeypatch.setattr(main,"cached_features",lambda:(features,{"last_sync":"now"}))
    monkeypatch.setattr(main,"_generation_context",lambda:(plex,lib))
    client=TestClient(main.app)
    exported=client.get("/api/collaboration/export.zip")
    assert exported.status_code==200,exported.text[:300] if not exported.is_success else ""
    assert exported.headers["content-type"]=="application/zip"
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        manifest=json.loads(archive.read("manifest.json"))
    data={"format":c.PROPOSAL_FORMAT,
          "source":{"library_revision":manifest["library_revision"],
                    "playlist_revision":manifest["playlist_revision"]},
          "playlists":[update()]}
    preview=client.post("/api/collaboration/proposals/preview",json=data)
    assert preview.status_code==200,preview.text
    assert original._ids==["1","2","3"]
    body={"proposal":preview.json()["proposal"],
          "review_token":preview.json()["review_token"],"approved":True}
    denied=client.post("/api/collaboration/proposals/apply",json=body)
    assert denied.status_code==403
    approved=client.post("/api/collaboration/proposals/apply",json=body,
                         headers={"Origin":"http://testserver"})
    assert approved.status_code==200,approved.text
    assert approved.json()["ok"]
    assert original._ids==["3","2","4"]
