"""Bounded, source-aware YouTube download recovery policy.

A new yt-dlp process re-extracts stream URLs on HTTP 403 or an explicit bot
challenge. Completed tracks remain protected by the download archive.
"""
from __future__ import annotations

import os

HTTP_403 = "http_403"
BOT_CHALLENGE = "bot_challenge"
RECOVERABLE = frozenset({HTTP_403, BOT_CHALLENGE})


def youtube_failure_kind(line):
    """Single classification for YouTube recovery, routing and diagnostics."""
    value = str(line).casefold()
    if "not a bot" in value and ("sign in to confirm" in value or "confirm you" in value):
        return BOT_CHALLENGE
    if "http error 403" in value or "http 403" in value:
        return HTTP_403
    return None


def _number(value, default, low, high):
    try:
        value = int(value)
    except (ValueError, TypeError, OverflowError):
        value = default
    return max(low, min(high, value))


def recovery_policy(environ=None):
    env = os.environ if environ is None else environ
    return {
        "restarts": _number(env.get("YOUTUBE_403_RESTARTS"), 2, 0, 3),
        "backoff_seconds": _number(env.get("YOUTUBE_403_BACKOFF_SECONDS"), 30, 5, 300),
        "track_pause_min": _number(env.get("YOUTUBE_TRACK_PAUSE_MIN"), 5, 0, 30),
        "track_pause_max": _number(env.get("YOUTUBE_TRACK_PAUSE_MAX"), 10, 0, 45),
        "request_pause": _number(env.get("YOUTUBE_REQUEST_PAUSE"), 1, 0, 10),
        "fallback_client": str(env.get("YOUTUBE_403_FALLBACK_CLIENT", "default") or "").strip(),
    }


def retry_delay(restart_index, policy):
    """First restart waits 30 seconds by default, second waits 90 seconds."""
    restart_index = max(1, int(restart_index))
    return min(900, policy["backoff_seconds"] * (3 ** (restart_index - 1)))


def player_client_for_attempt(base_client, attempt_index, policy):
    """Only the final permitted retry uses the optional alternative client."""
    if attempt_index == policy["restarts"] and attempt_index > 0:
        return policy["fallback_client"] or base_client
    return base_client


def track_pacing_args(policy):
    min_s = policy["track_pause_min"]
    max_s = max(min_s, policy["track_pause_max"])
    args = ["--retry-sleep", "http:exp=2:30",
            "--retry-sleep", "extractor:exp=2:30"]
    if policy["request_pause"]:
        args.extend(["--sleep-requests", str(policy["request_pause"])])
    if min_s:
        args.extend(["--sleep-interval", str(min_s),
                     "--max-sleep-interval", str(max_s)])
    return args


def should_restart(*, youtube, spotify, forced, returncode, failure_kind,
                   already_restarted, policy):
    return (youtube and not spotify and not forced and
            returncode != 0 and failure_kind in RECOVERABLE and
            already_restarted < policy["restarts"])


def needs_vpn_rotation(failure_kind, next_restart, policy):
    """Bot verification switches immediately; HTTP 403 switches on final retry."""
    return (failure_kind == BOT_CHALLENGE or
            (failure_kind == HTTP_403 and next_restart == policy["restarts"]))
