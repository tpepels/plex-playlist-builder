"""Read-only audit of real Plex playlist YAML against a Plex track export.

The resulting evidence is metadata-only. A short track or spoken-sounding
title is merely queued for manual review, never automatically deleted.
"""
from __future__ import annotations
from collections import Counter
import csv
from pathlib import Path
import statistics
import yaml
from .curation_review_rules import review_track


def load_export(playlists_file, tracks_file):
    data=yaml.safe_load(Path(playlists_file).read_text(encoding="utf-8"))
    playlists=data.get("playlists",data)
    dialect="\t" if str(tracks_file).lower().endswith(".tsv") else ","
    with Path(tracks_file).open(encoding="utf-8-sig",newline="") as stream:
        reader=csv.DictReader(stream,delimiter=dialect)
        required={"rating_key","artist","album","title"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("Track catalogue missing required columns.")
        tracks={str(row["rating_key"]):row for row in reader}
    return playlists,tracks


def audit_export(playlists,tracks):
    output={}
    for name,spec in playlists.items():
        keys=[str(item.get("rating_key") or "") for item in spec.get("items",[])]
        duplicates=[key for key,count in Counter(keys).items() if count>1]
        missing=[key for key in keys if key not in tracks]
        mismatches=[]
        spoken=[]
        short=[]
        durations=[]
        for pos,item in enumerate(spec.get("items",[])):
            key=keys[pos]
            if key not in tracks:continue
            row=tracks[key]
            for source,dest in (("artist","artist"),("album","album"),("track","title")):
                if source in item and str(item[source])!=str(row.get(dest,"")):
                    mismatches.append({"index":pos,"rating_key":key,"field":source,
                        "saved":item[source],"catalogue":row.get(dest,"")})
            issues=review_track(row)
            if any(flag["code"]=="likely_spoken_or_applause" for flag in issues):
                spoken.append({"rating_key":key,"artist":row["artist"],"title":row["title"]})
            if any(flag["code"]=="short_duration" for flag in issues):
                short.append({"rating_key":key,"artist":row["artist"],"title":row["title"],
                              "duration_ms":row.get("duration_ms")})
            try:
                milliseconds=int(row.get("duration_ms") or 0)
                if milliseconds>0:durations.append(milliseconds)
            except (ValueError,TypeError):
                pass
        output[name]={
            "tracks":len(keys),"resolved":len(keys)-len(missing),
            "missing_rating_keys":missing,"duplicate_rating_keys":duplicates,
            "metadata_mismatches":mismatches,"spoken_or_applause_title_candidates":spoken,
            "short_tracks_for_review":short,
            "distinct_artists":len({tracks[key]["artist"] for key in keys if key in tracks}),
            "median_duration_minutes":round(statistics.median(durations)/60000,2) if durations else None,
        }
    return {
        "metadata_only":True, "audio_reviewed":False, "live_plex_verified":False,
        "playlists":output,"playlist_count":len(playlists),"catalogue_tracks":len(tracks),
        "placements":sum(item["tracks"] for item in output.values()),
        "totals":{category:sum(len(v[category]) for v in output.values()) for category in
            ("missing_rating_keys","duplicate_rating_keys","metadata_mismatches",
             "spoken_or_applause_title_candidates","short_tracks_for_review")},
    }
