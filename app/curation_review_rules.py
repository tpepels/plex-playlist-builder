"""Conservative musical-review checks grounded only in Plex metadata.

Flag track titles that strongly indicate speech/applause, but do not classify
short instrumental pieces as errors. Explicit catalogue searches can still show
flagged recordings; no playback, deletions or automatic playlist edits happen.
"""
from __future__ import annotations

import re

NON_MUSICAL_TITLE = re.compile(
    r"\b(?:spoken[\s-]+(?:introduction|interlude|word)|interview|"
    r"audience[\s-]+applause|applause(?:[\s-]+and[\s-]+cheers)?|"
    r"band[\s-]+introductions?|stage[\s-]+announcements?|crowd[\s-]+noise)\b",
    re.IGNORECASE,
)


def review_track(info: dict) -> list[dict]:
    notes = []
    if NON_MUSICAL_TITLE.search(str(info.get("title") or "")):
        notes.append({
            "code": "likely_spoken_or_applause",
            "message": "Track title indicates speech/applause rather than a musical performance",
            "severity": "review",
        })
    duration = info.get("duration_ms")
    try:
        milliseconds = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        milliseconds = None
    if milliseconds is not None and 0 < milliseconds < 120000:
        notes.append({
            "code": "short_duration",
            "message": "Under two minutes; verify the musical purpose before removing",
            "severity": "informational",
        })
    return notes


def automatically_rankable(info: dict) -> bool:
    return not any(note["code"] == "likely_spoken_or_applause"
                   for note in review_track(info))
