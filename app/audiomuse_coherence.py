"""Exploratory MusiCNN playlist similarity, without automatic curation decisions.

A score is withheld until adequate coverage exists. It is an audio-similarity
diagnostic, NOT a calibrated quality, genre or accessibility assessment.
"""
from __future__ import annotations

import base64
import binascii
import math
import statistics
import struct


MIN_ANALYZED = 6
MIN_COVERAGE = 0.60
MAX_EMBEDDING_DIM = 4096


def vector_from_feature(feature: dict) -> tuple[float, ...] | None:
    encoded = feature.get("embedding")
    if not isinstance(encoded, str) or not encoded:
        return None
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(raw) < 16 or len(raw) % 4 or len(raw) > MAX_EMBEDDING_DIM * 4:
        return None
    values = struct.unpack("<" + "f" * (len(raw) // 4), raw)
    if not all(math.isfinite(v) for v in values):
        return None
    magnitude = math.sqrt(sum(v * v for v in values))
    if magnitude <= 1e-12:
        return None
    return tuple(v / magnitude for v in values)


def _dot(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _relative_outliers(entries: list[tuple[str, dict, tuple[float, ...]]]) -> tuple[float, list[dict]]:
    """Rank tracks by similarity with the average of OTHER playlist tracks."""
    size = len(entries)
    dimensions = len(entries[0][2])
    aggregate = [0.0] * dimensions
    for _, _, vector in entries:
        for i, value in enumerate(vector):
            aggregate[i] += value
    similarities = []
    for track_id, feature, vector in entries:
        rest = [aggregate[i] - vector[i] for i in range(dimensions)]
        rest_norm = math.sqrt(sum(value * value for value in rest))
        similarity = sum(a * b for a, b in zip(vector, rest)) / rest_norm if rest_norm > 1e-9 else 0.0
        similarities.append((similarity, track_id, feature))
    median = statistics.median(item[0] for item in similarities)
    ranked = sorted(similarities)
    candidates = [
        {
            "id": track_id,
            "artist": feature.get("artist") or "",
            "title": feature.get("title") or "",
            "similarity_to_others": round(similarity, 3),
            "gap_below_median": round(median - similarity, 3),
        }
        for similarity, track_id, feature in ranked
        if median - similarity >= 0.08
    ][:5]
    # Expected pairwise cosine from leave-one-out mean and aggregate; exact
    # mean across unique pairs, not centroid similarity.
    norm_squared = sum(value * value for value in aggregate)
    pairwise_mean = (norm_squared - size) / (size * (size - 1))
    return round(max(-1.0, min(1.0, pairwise_mean)), 3), candidates


def coherence_report(playlists: dict, cache: dict[str, dict], state: dict) -> dict:
    decoded = {}
    dimension = None
    for track_id, feature in cache.items():
        vector = vector_from_feature(feature)
        if vector is None:
            continue
        if dimension is None:
            dimension = len(vector)
        if len(vector) == dimension:
            decoded[str(track_id)] = vector

    reports = []
    for name, playlist in playlists.items():
        items = playlist.get("items", [])
        ids = list(dict.fromkeys(str(item.get("rating_key", "")).strip()
                                 for item in items if item.get("rating_key")))
        # Do not invent the expansion of a whole-album or title-only item.
        unresolved = sum(not item.get("rating_key") for item in items)
        total = len(ids)
        analyzed = [track_id for track_id in ids if track_id in cache]
        embedded = [(track_id, cache[track_id], decoded[track_id])
                    for track_id in ids if track_id in decoded]
        coverage = len(embedded) / total if total else 0.0
        status = "ready" if total > 0 and not unresolved and (
            len(embedded) >= MIN_ANALYZED and coverage >= MIN_COVERAGE
        ) else "insufficient"
        similarity, outliers = _relative_outliers(embedded) if status == "ready" else (None, [])
        reports.append({
            "name": name,
            "total_explicit_tracks": total,
            "unresolved_items": unresolved,
            "analyzed_tracks": len(analyzed),
            "embedded_tracks": len(embedded),
            "coverage": round(coverage, 3),
            "status": status,
            "mean_pairwise_cosine": similarity,
            "review_candidates": outliers,
        })
    return {
        "source": "AudioMuse-AI / Plex",
        "method": "MusiCNN embedding pairwise cosine, relative leave-one-out deviations",
        "notice": (
            "Exploratory audio similarity only. Missing/low coverage produces no score. "
            "Low similarity is a review candidate, not proof that music is unsuitable."
        ),
        "cache": {**state, "with_embeddings": len(decoded)},
        "summary": {
            "playlists": len(reports),
            "scorable": sum(item["status"] == "ready" for item in reports),
            "insufficient": sum(item["status"] == "insufficient" for item in reports),
        },
        "playlists": reports,
    }
