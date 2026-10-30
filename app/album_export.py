"""Compact read-only Plex album catalog export for music discovery."""
from __future__ import annotations

import csv
import io


def export_album_names(tracks) -> tuple[str, int]:
    """Return artist/album TSV and number of distinct artist-album pairs.

    The names are read from Plex track metadata. Repeat tracks, disc splits
    and duplicate releases with the same displayed artist and album collapse
    to one row; albums by different artists remain separate.
    """
    albums: dict[tuple[str, str], tuple[str, str]] = {}
    for track in tracks:
        artist = str(getattr(track, "grandparentTitle", "") or "").strip()
        album = str(getattr(track, "parentTitle", "") or "").strip()
        if not album:
            continue  # No album title to compare to new discoveries.
        key = (artist.casefold(), album.casefold())
        albums.setdefault(key, (artist, album))

    sorted_albums = sorted(
        albums.values(),
        key=lambda item: (item[0].casefold(), item[1].casefold(), item[0], item[1]),
    )
    output = io.StringIO()
    writer = csv.writer(output, delimiter="\t", lineterminator="\n")
    writer.writerow(("artist", "album"))
    writer.writerows(sorted_albums)
    return output.getvalue(), len(sorted_albums)
