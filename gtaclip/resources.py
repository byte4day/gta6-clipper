"""Shared CPU/IO gates so clip renders cannot starve live recorders."""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager

# One heavy encode at a time by default (8-core / 8 GB boxes fall over with 2× boxblur+x264).
_SLOTS = max(1, int(os.environ.get("GTACLIP_ENCODE_SLOTS", "1")))
_encode = threading.Semaphore(_SLOTS)
_busy = 0
_lock = threading.Lock()
_ai_scans = threading.Semaphore(1)  # at most one AI moment-scan ffmpeg/whisper at a time


def encoding() -> bool:
    with _lock:
        return _busy > 0


@contextmanager
def encode_slot():
    """Hold while running a heavy ffmpeg encode (cut fallback, vertical render)."""
    global _busy
    _encode.acquire()
    with _lock:
        _busy += 1
    try:
        yield
    finally:
        with _lock:
            _busy -= 1
        _encode.release()


@contextmanager
def ai_scan_slot(blocking: bool = False):
    """Serialize AI scans; skip if a scan (or optionally encode) is already running."""
    if encoding():
        yield False
        return
    got = _ai_scans.acquire(blocking=blocking)
    if not got:
        yield False
        return
    try:
        yield True
    finally:
        _ai_scans.release()
