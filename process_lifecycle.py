"""Bounded subprocess-inactivity detection for yt-dlp and spotDL.

Progress messages reset a monotonic deadline. A silent, blocked child is
terminated, then killed after a short grace period so one hung source cannot
hold the single download worker indefinitely. Test doubles without process
control methods run without a background monitor.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time


def inactivity_limit(environ=None):
    env = os.environ if environ is None else environ
    try:
        seconds = int(env.get("DOWNLOAD_STALL_SECONDS", "900"))
    except (ValueError, TypeError, OverflowError):
        seconds = 900
    return max(120, min(3600, seconds))


def _signal(process, sig):
    """Signal the dedicated downloader process group, including ffmpeg."""
    pid = getattr(process, "pid", None)
    if isinstance(pid, int) and pid > 0:
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass
    else:
        # Unit-test processes may not expose PIDs; never signal a guessed PGID.
        operation = "terminate" if sig == signal.SIGTERM else "kill"
        method = getattr(process, operation, None)
        if callable(method):
            method()


def stop_process(process, grace=5):
    """Never leave a child downloader running after a job exception."""
    poll = getattr(process, "poll", None)
    if not callable(poll) or poll() is not None:
        return
    _signal(process, signal.SIGTERM)
    wait = getattr(process, "wait", None)
    if not callable(wait):
        return
    try:
        wait(timeout=grace)
    except subprocess.TimeoutExpired:
        _signal(process, signal.SIGKILL)
        try:
            wait(timeout=grace)
        except subprocess.TimeoutExpired:
            # The child cannot be reaped; retain the job failure and let
            # the container supervisor perform the remaining cleanup.
            pass


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
                # Parent yt-dlp may have exited while ffmpeg still holds
                # stdout open. Only the completed *reading context* confirms
                # that the whole process group has finished; keep monitoring
                # silence rather than relying solely on parent poll().
                if time.monotonic() - self._last < self.seconds:
                    continue
                self.timed_out = True
                _signal(self.process, signal.SIGTERM)
                if not self._stop.wait(self.grace):
                    pid = getattr(self.process, "pid", None)
                    if (isinstance(pid, int) and pid > 0
                            or self.process.poll() is None):
                        _signal(self.process, signal.SIGKILL)
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
