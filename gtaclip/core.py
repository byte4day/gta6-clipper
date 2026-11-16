"""Bridge to the Rust helper `gtacore` (core/). Every function has a pure-Python fallback,
so the clipper still runs when the binary is missing; it is just slower.

  live_status()  : parallel live checks from public channel pages (no streamlink per offline streamer)
  ts_duration()  : MPEG-TS duration from PES timestamps (~25x faster than spawning ffprobe)
  ChatHub        : one Twitch IRC socket for every channel instead of one thread + socket each
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

from .config import ROOT

log = logging.getLogger("core")


def _find_bin() -> str | None:
    env = os.environ.get("GTACORE_BIN")
    for cand in (env, ROOT / "core/target/release/gtacore", ROOT / "bin/gtacore", shutil.which("gtacore")):
        if cand and Path(cand).is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


BIN = _find_bin()


def available() -> bool:
    return BIN is not None


def live_status(targets: list[tuple[str, str]], timeout: float = 25) -> dict[str, dict]:
    """{"twitch:xqc": {"live": bool, "id": str}} for each (platform, name). Errors are left out."""
    if not BIN or not targets:
        return {}
    args = [f"{p}:{n}" for p, n in targets]
    try:
        r = subprocess.run([BIN, "live", *args], capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("gtacore live: %s", e)
        return {}
    out = {}
    for line in r.stdout.splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "error" not in d:
            out[d["target"]] = {"live": bool(d.get("live")), "id": d.get("id") or ""}
    return out


def ts_duration(path: Path) -> float | None:
    if not BIN:
        return None
    try:
        r = subprocess.run([BIN, "tsdur", str(path)], capture_output=True, text=True, timeout=10)
        return float(json.loads(r.stdout.splitlines()[0]).get("duration") or 0) or None
    except (subprocess.SubprocessError, OSError, ValueError, IndexError, json.JSONDecodeError):
        return None


class ChatHub:
    """Single `gtacore chat` process shared by every Twitch chat reader."""

    _inst: "ChatHub | None" = None
    _lock = threading.Lock()

    @classmethod
    def get(cls, hype_words: list[str]) -> "ChatHub | None":
        if not BIN:
            return None
        with cls._lock:
            if cls._inst is None or cls._inst.proc.poll() is not None:
                cls._inst = ChatHub(hype_words)
            return cls._inst

    def __init__(self, hype_words: list[str]):
        self.subs: dict[str, Callable[[str, str], None]] = {}
        self.connected = False
        self.proc = subprocess.Popen([BIN, "chat", "--hype", ",".join(hype_words)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.wlock = threading.Lock()
        threading.Thread(target=self._read, daemon=True, name="chat-hub").start()

    def _send(self, line: str) -> None:
        with self.wlock:
            try:
                self.proc.stdin.write(line + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    def join(self, channel: str, cb: Callable[[str, str], None]) -> None:
        self.subs[channel.lower()] = cb
        self._send(f"join {channel.lower()}")

    def part(self, channel: str) -> None:
        self.subs.pop(channel.lower(), None)
        self._send(f"part {channel.lower()}")

    def _read(self) -> None:
        for line in self.proc.stdout:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "ev" in d:
                self.connected = d["ev"] == "connected"
                continue
            cb = self.subs.get(d.get("c", ""))
            if cb:
                cb(d.get("u", ""), d.get("t", ""))
        self.connected = False
