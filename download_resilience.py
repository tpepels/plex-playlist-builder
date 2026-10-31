"""Conservative YouTube HTTP 403 recovery policy, independent of Flask.

A new process re-extracts the stream URL; retrying the rejected URL alone
may repeat the same 403. Completed tracks are protected by yt-dlp's archive
and --no-overwrites. All settings are bounded to avoid accidental retry storms.
"""
from __future__ import annotations

import os


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
        "fallback_client": str(env.get("YOUTUBE_403_FALLBACK_CLIENT", "web_safari") or "").strip(),
    }


def retry_delay(restart_index, policy):
    """First restart waits 30 s by default; second waits 90 s."""
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


def should_restart(*, youtube, spotify, forced, returncode, got_http_403,
                   already_restarted, policy):
    return (youtube and not spotify and not forced and
            returncode != 0 and got_http_403 and
            already_restarted < policy["restarts"])
