"""Explainable playlist composition and sequencing review.

Measures only observed AudioMuse vectors/metadata. Does not synthesize missing
features or claim a calibrated musical-quality probability.
"""
from __future__ import annotations

from collections import Counter
from statistics import mean

from .playlist_curator import features_for
from .playlist_generation import transition_diagnostics


def evaluate(keys, library, raw_features, existing=None, *, exclude_name=None):
    features=features_for({str(k):raw_features[str(k)]
                           for k in keys if str(k) in raw_features})
    valid=[str(k) for k in keys if str(k) in library]
    total=len(valid)
    artists=Counter(str(library[k].get("artist") or "") for k in valid)
    albums=Counter(str(library[k].get("album_key") or library[k].get("album") or "") for k in valid)
    edges=transition_diagnostics(valid,features)
    measured_similarity=[t["audio_similarity"] for t in edges if t["audio_similarity"] is not None]
    measured_energy=[t["energy_jump"] for t in edges if t["energy_jump"] is not None]
    adjacent_artists=sum(library[a].get("artist")==library[b].get("artist")
                         for a,b in zip(valid,valid[1:]))
    adjacent_albums=sum(library[a].get("album_key")==library[b].get("album_key")
                        for a,b in zip(valid,valid[1:]))
    full_vectors=sum(bool(features.get(k,{}).get("musicnn") and
                          features.get(k,{}).get("clap")) for k in valid)
    warnings=[]
    if total and max(artists.values())/total>.4 and total>=6:
        warnings.append("High artist concentration")
    if total and max(albums.values())/total>.3 and total>=6:
        warnings.append("High album concentration")
    if total>=3 and adjacent_artists>=max(2,(total-1)//3):
        warnings.append("Repeated adjacent artists")
    if measured_similarity and min(measured_similarity)<.57:
        warnings.append("Some adjacent recordings have low AudioMuse similarity")
    if measured_energy and max(measured_energy)>.35:
        warnings.append("Some transitions have large measured energy changes")
    if len(measured_similarity)<max(0,total-1):
        warnings.append("Some sequence transitions lack usable embeddings")
    if total and full_vectors<total:
        warnings.append("Missing MusicNN or CLAP embeddings; coherence conclusions are limited")
    overlaps=[]
    proposed=set(valid)
    for name,spec in (existing or {}).items():
        if name==exclude_name:
            continue
        source=[str(row.get("rating_key")) for row in spec.get("items",[])
                if isinstance(row,dict) and row.get("rating_key")]
        known=set(source)
        if not known or not proposed:continue
        shared=len(proposed & known)
        if not shared:continue
        overlaps.append({"name":name,"common_tracks":shared,
                         "jaccard":round(shared/len(proposed|known),4),
                         "fraction_of_proposal":round(shared/len(proposed),4),
                         "identity_coverage":len(source)==len(spec.get("items",[]))})
    overlaps.sort(key=lambda x:(-x["fraction_of_proposal"],-x["jaccard"],x["name"]))
    if any(o["fraction_of_proposal"]>=.8 for o in overlaps):
        warnings.append("Playlist substantially overlaps an existing collection")
    return {
        "tracks":total,
        "unique_artists":len(artists),
        "unique_albums":len(albums),
        "max_artist_share":round(max(artists.values())/total,4) if total else None,
        "max_album_share":round(max(albums.values())/total,4) if total else None,
        "adjacent_same_artist":adjacent_artists,
        "adjacent_same_album":adjacent_albums,
        "full_embedding_coverage":{"tracks":full_vectors,"total":total},
        "audio_similarity":{"mean":round(mean(measured_similarity),4)
                            if measured_similarity else None,
                            "minimum":min(measured_similarity) if measured_similarity else None,
                            "measured_transitions":len(measured_similarity),
                            "total_transitions":len(edges)},
        "energy_changes":{"mean":round(mean(measured_energy),4)
                          if measured_energy else None,
                          "maximum":max(measured_energy) if measured_energy else None,
                          "measured_transitions":len(measured_energy),
                          "total_transitions":len(edges)},
        "transitions":edges,
        "existing_overlaps":overlaps[:10],
        "warnings":warnings,
        "measurement_note":"Relative audio-feature measurements only; unknown values are never estimated.",
    }
