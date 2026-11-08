"""Bounded subprocess-inactivity detection for yt-dlp and spotDL.

Progress messages reset a monotonic deadline. A silent, blocked child is
terminated, then killed after a short grace period so one hung source cannot
hold the single download worker indefinitely. Test doubles without process
control methods run without a background monitor.
"""
from __future__ import annotations

import os
import threading
import time


def inactivity_limit(environ=None):
    env = os.environ if environ is None else environ
    try:
        seconds = int(env.get("DOWNLOAD_STALL_SECONDS", "900"))
    except (ValueError, TypeError, OverflowError):
        seconds = 900
    return max(120, min(3600, seconds))


class InactivityGuard:
    def __init__(self, process, *, seconds=900, interval=5, grace=10):
        self.process = process
        self.seconds = seconds
        self.interval = interval
        self.grace = grace
        self.timed_out = False
        self._last = time.monotonic()
        self._stop = threading.Event()
        self._thread = None

    def beat(self):
        self._last = time.monotonic()

    def _watch(self):
        while not self._stop.wait(self.interval):
            try:
                if self.process.poll() is not None:
                    return
                if time.monotonic() - self._last < self.seconds:
                    continue
                self.timed_out = True
                self.process.terminate()
                if not self._stop.wait(self.grace) and self.process.poll() is None:
                    self.process.kill()
                return
            except (OSError, ProcessLookupError):
                # The process may have exited between poll and terminate.
                return

    def __enter__(self):
        if all(callable(getattr(self.process, operation, None))
               for operation in ("poll", "terminate", "kill")):
            self._thread = threading.Thread(
                target=self._watch, name="download-inactivity-guard",
                daemon=True,
            )
            self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        return False
