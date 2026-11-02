#!/usr/bin/env python3
"""Audit a complete Plex export without modifying playlists or making audio claims."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.curation_export_audit import audit_export, load_export


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--playlists",required=True,help="YAML playlist definitions")
    parser.add_argument("--library",required=True,help="TSV or CSV Plex music export")
    parser.add_argument("--output",required=True,help="Report destination (.json)")
    options=parser.parse_args()
    playlists,tracks=load_export(options.playlists,options.library)
    result=audit_export(playlists,tracks)
    Path(options.output).write_text(
        json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print("Playlists",result["playlist_count"],
          "placements",result["placements"],
          "library tracks",result["catalogue_tracks"])
    for key,value in result["totals"].items():print(key,value)


if __name__=="__main__":
    main()
