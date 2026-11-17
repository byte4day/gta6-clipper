"""Per-streamer watcher: live detection, recording, moment detection, clip jobs."""
from __future__ import annotations

import itertools
import json
import logging
import re
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import core
from .avatars import avatar_path, fetch_avatar
from .chat import ChatBase, make_chat
from .config import ROOT, Config, Streamer
from .editor import process_clip
from .llm import LLM, scan_moment
from .recorder import STREAMLINK, Recorder
from .resources import ai_scan_slot, encoding
from .socials import auto_upload_if_enabled
from .transcribe import transcribe

log = logging.getLogger("watch")
_ids = itertools.count(1)


def probe(streamer: Streamer) -> dict:
    """{'live': bool, 'title', 'category'} via streamlink's public plugin metadata."""
    try:
        r = subprocess.run([STREAMLINK, "--json", streamer.url], capture_output=True, text=True, timeout=45)
        d = json.loads(r.stdout or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return {"live": False, "title": "", "category": "", "error": "probe failed"}
    md = d.get("metadata") or {}
    return {"live": bool(d.get("streams")), "title": md.get("title") or "", "category": md.get("category") or "", "id": md.get("id") or ""}


@dataclass
class ClipJob:
    trigger: str
    reason: str
    t0: float
    t1: float
    id: int = field(default_factory=lambda: next(_ids))
    created: float = field(default_factory=time.time)


class Watcher:
    def __init__(self, streamer: Streamer, app: "App"):
        self.s = streamer
        self.app = app
        cfg = app.cfg
        self.cfg = cfg
        self.d = cfg["detect"]
        self.state_file = ROOT / "state" / f"{streamer.key}.json"
        self.state = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        self.gta_re = re.compile(self.d.get("gta_pattern", r"gta ?6|grand theft auto vi"), re.I)
        self.live = False      # we are recording this stream
        self.online = False    # streamer is live on the platform (recorded or not)
        self.title = self.category = ""
        self.live_since = 0.0
        self.rec: Recorder | None = None
        self.chat: ChatBase | None = None
        self.pending: list[ClipJob] = []
        self.last_auto = 0.0
        self.loud_avg: float | None = None
        self.last_scan = 0.0
        self.events: list[str] = []
        self.lock = threading.Lock()
        self.first_poll = True
        self.offline_since = 0.0
        self.video_id = ""
        self.online_since = 0.0
        self.status = "idle"
        self.removed = threading.Event()
        self.rate_hist: deque = deque(maxlen=150)  # (t, chat msgs/sec) every 2 s for sparklines
        self.clip_count = 0
        self.hold_off = False  # user stopped this recording: don't restart until the stream ends
        self.force = False  # "record anyway" from the dashboard (ignores gta_only / max_recordings)
        self.wake = threading.Event()
        self.last_full_probe = 0.0
        self._scan_busy = False  # true while an AI-scan thread is alive (even if it ends up skipping)

    # ---- helpers ----------------------------------------------------------
    def note(self, msg: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {msg}"
        self.events = (self.events + [line])[-30:]
        log.info("[%s] %s", self.s.display, msg)

    def save_state(self) -> None:
        self.state_file.parent.mkdir(exist_ok=True)
        self.state_file.write_text(json.dumps(self.state, indent=2))

    def is_gta(self) -> bool:
        return bool(self.gta_re.search(f"{self.category} {self.title}"))

    def snapshot(self) -> dict:
        rec = self.rec
        return {
            "name": self.s.display, "key": self.s.key, "platform": self.s.platform, "url": self.s.url,
            "enabled": self.s.enabled, "live": self.live or self.online, "online_since": self.online_since, "status": self.status, "title": self.title,
            "category": self.category, "gta": self.is_gta(), "recording": bool(rec and rec.alive()),
            "buffer_s": round(rec.buffered_until() - rec.buffered_from()) if rec and rec.alive() else 0,
            "chat_rate": round(self.chat.rate(5), 1) if self.chat else 0,
            "chat_base": round(self.chat.baseline or 0, 1) if self.chat else 0,
            "loud": round(self.loud_avg or 0, 1), "pending": len(self.pending),
            "gta6_first_done": bool(self.state.get("gta6_first")), "events": self.events[-8:],
            "avatar": avatar_path(self.s.key).exists(), "live_since": self.live_since if self.live else 0,
            "backend": rec.backend if rec else "", "quality": rec.quality_name if rec else "",
            "in_ad": bool(rec and rec.in_ad), "preview": bool(rec and rec.alive() and rec.preview().exists()),
            "mbps": round(rec.bytes_in * 8 / 1e6 / max(1, time.time() - rec.started_at), 1) if rec and rec.bytes_in else 0,
            "spark": [r for _, r in list(self.rate_hist)[-60:]], "clips": self.clip_count,
            "chat_ok": bool(self.chat and self.chat.connected),
        }

    # ---- triggers ---------------------------------------------------------
    def fire(self, trigger: str, reason: str, t0: float, t1: float, auto: bool = True) -> bool:
        if not (self.rec and self.rec.alive()):
            return False
        now = time.time()
        with self.lock:
            for j in self.pending:  # merge into an overlapping clip instead of duplicating
                if t0 <= j.t1 and t1 >= j.t0 and j.trigger not in ("stream_start", "gta6_open"):
                    j.t1 = min(max(j.t1, t1), j.t0 + 60)  # hard cap keeps renders fast under load
                    if reason not in j.reason:
                        j.reason += f" + {reason}"
                    return True
            if auto and trigger in ("chat_spike", "audio_spike", "ai_moment"):
                if now - self.last_auto < float(self.d.get("cooldown_seconds", 45)):
                    return False
                self.last_auto = now
            if len(self.pending) >= 8:  # never let a backlog pin the whole ring buffer in RAM/disk
                return False
            t0 = max(t0, self.rec.buffered_from())
            t1 = min(t1, t0 + 90)
            job = ClipJob(trigger, reason, t0, t1)
            self.pending.append(job)
            self.rec.holds[job.id] = t0
        self.note(f"✂ {trigger}: {reason}")
        return True

    def fire_window(self, trigger: str, reason: str, at: float | None = None, auto: bool = True) -> bool:
        pre, post = self.cfg.pre_post(trigger)
        at = at or time.time()
        return self.fire(trigger, reason, at - pre, at + post, auto)

    def on_loudness(self, a: float, b: float, peak: float) -> None:
        if self.loud_avg is None:
            self.loud_avg = peak
            return
        jump = peak - self.loud_avg
        if jump >= float(self.d.get("audio_spike_db", 9)) and time.time() - self.live_since > 30:
            self.fire_window("audio_spike", f"loudness +{jump:.0f} dB", at=(a + b) / 2)
        self.loud_avg = self.loud_avg * 0.92 + peak * 0.08

    def check_chat(self) -> None:
        c = self.chat
        if c and (not self.rate_hist or time.time() - self.rate_hist[-1][0] >= 2):
            self.rate_hist.append((time.time(), round(c.rate(5), 1)))
        if not c or not c.warm():
            return
        base = c.update_baseline()
        rate = c.rate(5)
        need = max(float(self.d.get("chat_spike_min_rate", 4)), base * float(self.d.get("chat_spike_ratio", 3)))
        hype = c.hype_ratio(6)
        if rate >= need or (rate >= need * 0.6 and hype >= 0.5):
            # Center on the loudest recent chat sample, not "now" (spike already happened).
            recent = list(self.rate_hist)[-10:]
            peak_t = max(recent, key=lambda x: x[1])[0] if recent else time.time()
            self.fire_window(
                "chat_spike",
                f"chat {rate:.0f}/s (normal {base:.1f}/s, hype {hype:.0%})",
                at=peak_t,
            )

    def ai_scan(self) -> bool:
        """Run one moment scan. Returns True only if the scan actually ran (slot acquired)."""
        llm = self.app.llm
        if not (self.cfg["llm"].get("ai_scan", True) and llm.available() and self.rec):
            return False
        with ai_scan_slot(blocking=False) as ok:
            if not ok:
                return False  # busy — caller must NOT burn the scan cooldown
            self.last_scan = time.time()
            end = self.rec.buffered_until()
            if end - self.rec.buffered_from() < 30:
                return True
            win0 = end - 30
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td) / "scan.mp4"
                # Fast copy is fine here — Whisper only needs audio.
                if not self.rec.cut(win0, end, tmp, accurate=False):
                    return True
                tr = transcribe(self.cfg, tmp)
            chat = [t for _, _, t in self.chat.between(win0, end + 8)] if self.chat else []
            res = scan_moment(llm, self.s.display, tr["text"], chat, self.category)
            if not res or int(res.get("score", 0)) < int(self.cfg["llm"].get("ai_min_score", 8)):
                return True
            try:
                peak_at = float(res.get("peak_at", 22))
            except (TypeError, ValueError):
                peak_at = 22.0
            peak_at = max(0.0, min(30.0, peak_at))
            at = win0 + peak_at
            self.fire_window("ai_moment", f"AI {res.get('score')}/10: {res.get('reason', '')}", at=at)
            return True

    # ---- lifecycle ----------------------------------------------------------
    def go_live(self, fresh: bool) -> None:
        # Submit any offline clips first; never delete a buffer mid-cut.
        self.flush_jobs()
        old = self.rec
        if old:
            old.stop()
            if old.holds:
                # Late make_clip jobs still need the segments — move them aside so the
                # new session can record into buffer/<key>/ without racing the cuts.
                self._retire_recorder(old)
            else:
                try:
                    old.cleanup()
                except Exception as e:
                    log.warning("buffer cleanup %s: %s", self.s.display, e)
            if self.rec is old:
                self.rec = None
        self.pending.clear()
        self.live = True
        self.live_since = time.time()
        self.rec = Recorder(self.s, self.cfg, on_loudness=self.on_loudness)
        self.rec.start()
        self.chat = make_chat(self.s, self.d.get("hype_words", []), self.video_id)
        self.loud_avg = None
        self.status = "live"
        if fresh:
            n = float(self.d.get("first_moment_seconds", 75))
            self.note("went LIVE → clipping first moment")
            self.fire("stream_start", "stream just started", self.live_since, self.live_since + n, auto=False)
        else:
            self.note("already live when tool started (no first-moment clip)")
        self.check_gta6_first()

    def go_offline(self, why: str = "") -> None:
        self.note("stream ended" + (f" ({why})" if why else ""))
        self.live = False
        self.offline_since = time.time()
        self.status = "offline"
        if self.chat:
            self.chat.stop()
            self.chat = None
        # Stop ingest but keep segments on disk until pending clips finish cutting.
        if self.rec:
            self.rec.stop()

    def _retire_recorder(self, rec: Recorder) -> None:
        """Rename the ring buffer so reconnects can record while late cuts finish."""
        src = rec.dir
        if not src.exists():
            return
        dst = src.parent / f"{src.name}.retiring-{int(time.time() * 1000)}"
        try:
            src.rename(dst)
            rec.dir = dst
            log.info("retired buffer %s → %s (clips still cutting)", src.name, dst.name)
        except OSError as e:
            log.warning("could not retire buffer %s: %s", src, e)

    def _release_recorder(self, force: bool = False) -> None:
        """Free buffer disk/RAM once nothing still needs the ring segments."""
        rec = self.rec
        if not rec:
            return
        if not force and (self.live or self.pending or rec.holds):
            return
        try:
            rec.cleanup()
        except Exception as e:
            log.warning("buffer cleanup %s: %s", self.s.display, e)
        if self.rec is rec:
            self.rec = None

    def check_gta6_first(self) -> None:
        if self.is_gta() and not self.state.get("gta6_first"):
            self.state["gta6_first"] = datetime.now().isoformat(timespec="seconds")
            self.save_state()
            n = float(self.d.get("gta6_open_seconds", 90))
            self.note("FIRST TIME on GTA 6 → clipping")
            self.fire("gta6_open", f"first time opening GTA 6 ({self.category})", time.time() - 5, time.time() + n, auto=False)

    def should_record(self, info: dict) -> bool:
        if self.cfg["general"].get("record_mode", "always") == "gta_only":
            return bool(self.gta_re.search(f"{info['category']} {info['title']}"))
        return True

    def run(self) -> None:
        poll = float(self.cfg["general"].get("poll_seconds", 45))
        next_probe = 0.0
        last_avatar = 0.0
        while not self.app.stopping.is_set() and not self.removed.is_set():
            if not avatar_path(self.s.key).exists() and time.time() - last_avatar > 600:
                last_avatar = time.time()  # retry missing profile pictures every 10 min
                threading.Thread(target=fetch_avatar, args=(self.s,), daemon=True).start()
            if not self.s.enabled:
                if self.live:
                    self.go_offline()
                self.flush_jobs()
                self.status = "disabled"
                self.online = False
                next_probe = 0.0
                self.app.stopping.wait(1.0)
                continue
            now = time.time()
            hint = self.app.live_hint(self.s)
            if now >= next_probe and hint is False and not self.live and now - self.last_full_probe < 300:
                # Rust page check says offline: skip the slow streamlink probe (a full probe still runs every 5 min)
                next_probe = now + poll
                self.online = False
                self.status = "offline"
                self.force = self.hold_off = False
                self.first_poll = False
            if now >= next_probe:
                self.last_full_probe = now
                info = probe(self.s)
                next_probe = now + (30 if self.live else poll)
                was_cat = self.category
                self.title, self.category = info["title"], info["category"]
                self.video_id = info.get("id") or self.video_id
                if info["live"] and not self.online:
                    self.online_since = time.time()
                self.online = info["live"]
                if info["live"] and not self.live:
                    if self.hold_off:
                        self.status = "live (stopped by you)"
                    elif not self.should_record(info) and not self.force:
                        self.status = "live (not GTA)"
                    elif not self.force and self.app.recording_count() >= int(self.cfg["general"].get("max_recordings", 6)):
                        self.status = "live (max_recordings reached)"
                    else:
                        # "fresh" = we saw it go offline->live ourselves (not a reconnect blip)
                        fresh = not self.first_poll and time.time() - self.offline_since > 300
                        self.go_live(fresh=fresh)
                elif self.live and not info["live"] and not (self.rec and self.rec.alive()):
                    self.go_offline("platform says offline")
                elif not info["live"]:
                    self.status = "offline"
                    self.force = self.hold_off = False
                if self.live and self.category != was_cat and was_cat:
                    self.note(f"category → {self.category}")
                if self.live:
                    self.check_gta6_first()
                self.first_poll = False
            if self.live:
                if self.rec and not self.rec.alive():
                    # streamlink died: stream ended or network blip; reprobe soon
                    if self.live and time.time() - self.live_since > 20:
                        self.go_offline(self.rec.why_dead())
                        next_probe = 0
                else:
                    self.check_chat()
                    if self._scan_due() and not encoding() and not self._scan_busy:
                        self._scan_busy = True
                        threading.Thread(target=self._safe_scan, daemon=True).start()
            self.flush_jobs()
            if not self.live:
                self._release_recorder()
            if self.wake.wait(1.0):
                self.wake.clear()
                next_probe = 0.0
        self._release_recorder(force=True)

    def _scan_due(self) -> bool:
        return (self.cfg["llm"].get("ai_scan", True) and self.app.llm.available()
                and time.time() - self.last_scan >= float(self.cfg["llm"].get("scan_seconds", 40)))

    def _safe_scan(self) -> None:
        try:
            self.ai_scan()  # sets last_scan only after acquiring the scan slot
        except Exception as e:
            log.warning("ai scan %s: %s", self.s.display, e)
        finally:
            self._scan_busy = False

    def flush_jobs(self) -> None:
        """Cut every pending clip whose end is now in the buffer (or the stream ended)."""
        if not self.rec:
            return
        rec = self.rec  # bind before submit — go_live may replace w.rec while the pool runs
        done_until = rec.buffered_until()
        dead = not rec.alive()
        with self.lock:
            ready = [j for j in self.pending if done_until >= j.t1 or dead]
            for j in ready:
                self.pending.remove(j)
        for j in ready:
            self.app.pool.submit(self.app.make_clip, self, j, rec)


def friendly_error(e: Exception) -> str:
    if isinstance(e, subprocess.CalledProcessError):
        return f"{Path(str(e.cmd[0])).name} failed (exit {e.returncode}); try Re-render"
    return str(e)[:160]


class App:
    def __init__(self, cfg: Config, only: list[str] | None = None):
        self.cfg = cfg
        self.llm = LLM(cfg)
        self.stopping = threading.Event()
        # Default 1 worker: parallel x264+blur encodes fight live HLS/streamlink on typical boxes.
        self.pool = ThreadPoolExecutor(max_workers=max(1, int(cfg["general"].get("clip_workers", 1))))
        streamers = list(cfg.streamers)
        if only:
            keys = {o.lstrip("@").lower() for o in only}
            streamers = [s for s in cfg.streamers if s.key in keys]
        self.watchers = [Watcher(s, self) for s in streamers]
        self.out = cfg.path("output_dir")
        self.edit_lock = threading.Lock()
        self.live_cache: dict[str, tuple[float, bool, str]] = {}  # "platform:name" -> (checked_at, live, video id)

    # ---- fast live checks (Rust) ---------------------------------------------
    def live_hint(self, s: Streamer) -> bool | None:
        """True/False from a recent gtacore page check, None if unknown (then streamlink decides)."""
        hit = self.live_cache.get(f"{s.platform}:{s.name}")
        if not hit or time.time() - hit[0] > 90:
            return None
        return hit[1]

    def _live_once(self) -> None:
        targets = [(w.s.platform, w.s.name) for w in list(self.watchers) if w.s.enabled]
        res = core.live_status(targets)
        now = time.time()
        for w in list(self.watchers):
            k = f"{w.s.platform}:{w.s.name}"
            if k not in res:
                continue
            was = self.live_cache.get(k)
            self.live_cache[k] = (now, res[k]["live"], res[k]["id"])
            if res[k]["live"] and was and not was[1] and not w.live:
                w.wake.set()  # just went live: probe + start recording right away

    def _live_loop(self) -> None:
        every = float(self.cfg["general"].get("live_check_seconds", 20))
        while not self.stopping.wait(every):
            self._live_once()

    def recording_count(self) -> int:
        return sum(1 for w in self.watchers if w.rec and w.rec.alive())

    def start(self) -> None:
        self._scrub_orphan_buffers()
        if core.available():
            log.info("gtacore (Rust) on: fast live checks, segment timing, one chat socket")
            self._live_once()  # one ~1 s check up front, so offline streamers never start a streamlink probe
            threading.Thread(target=self._live_loop, daemon=True, name="live-check").start()
        else:
            log.info("gtacore not built (cd core && cargo build --release); using Python fallbacks")
        for w in self.watchers:
            threading.Thread(target=w.run, daemon=True, name=f"watch-{w.s.key}").start()

    def _scrub_orphan_buffers(self) -> None:
        """Delete leftover buffer/<streamer>/ dirs from previous runs (can be hundreds of MB)."""
        import shutil

        root = self.cfg.path("buffer_dir")
        if not root.exists():
            return
        freed = 0
        for p in root.iterdir():
            if not p.is_dir():
                continue
            # Always wipe on startup — no recorder is alive yet.
            try:
                size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                shutil.rmtree(p, ignore_errors=True)
                freed += size
            except OSError:
                pass
        if freed:
            log.info("cleared %.0f MB of leftover ring buffers", freed / 1e6)

    def make_clip(self, w: Watcher, job: ClipJob, rec: Recorder | None = None) -> None:
        stamp = datetime.fromtimestamp(job.t0).strftime("%Y%m%d-%H%M%S")
        d = self.out / w.s.key / f"{stamp}_{job.trigger}"
        d.mkdir(parents=True, exist_ok=True)
        rec = rec or w.rec  # prefer the recorder bound at flush time (survives reconnect)
        try:
            if not rec or not rec.cut(job.t0, job.t1, d / "raw.mp4"):
                w.note(f"✗ {job.trigger}: nothing in buffer")
                return
            chat = [f"{u}: {t}" for _, u, t in (w.chat.between(job.t0, job.t1 + 6) if w.chat else [])]
            process_clip(self.cfg, self.llm, d, {
                "streamer": w.s.display, "platform": w.s.platform, "trigger": job.trigger, "reason": job.reason,
                "title_stream": w.title, "title": w.title, "category": w.category, "chat": chat[-80:],
                "started": datetime.fromtimestamp(job.t0).isoformat(timespec="seconds"),
                "length": round(job.t1 - job.t0, 1),
            })
            w.clip_count += 1
            w.note(f"✔ clip ready ({job.trigger})")
            self.pool.submit(auto_upload_if_enabled, self, d)
        except Exception as e:
            log.exception("clip failed")
            (d / "meta.json").write_text(json.dumps({"streamer": w.s.display, "trigger": job.trigger,
                                                     "status": "error", "error": friendly_error(e)}))
        finally:
            if rec:
                rec.holds.pop(job.id, None)
                # Retired recorder from a previous session (reconnect): free it once cuts finish.
                if rec is not w.rec and not rec.holds:
                    try:
                        rec.cleanup()
                    except Exception as e:
                        log.warning("retired buffer cleanup %s: %s", w.s.display, e)
                elif not w.live:
                    w._release_recorder()

    # ---- runtime changes from the dashboard -----------------------------------
    def find(self, key: str) -> Watcher | None:
        return next((w for w in self.watchers if w.s.key == key.lstrip("@").lower()), None)

    def add_streamer(self, platform: str, name: str) -> Watcher:
        name = name.strip().split("/")[-1] if "/" in name.strip().rstrip("/") else name.strip()
        if platform == "youtube" and not name.startswith(("@", "UC")):
            name = "@" + name
        with self.edit_lock:
            if self.find(name):
                raise ValueError(f"{name} is already in the list")
            s = Streamer(platform, name)
            self.cfg.streamers.append(s)
            self.cfg.save()
            w = Watcher(s, self)
            self.watchers.append(w)
        threading.Thread(target=w.run, daemon=True, name=f"watch-{w.s.key}").start()
        return w

    def remove_streamer(self, key: str) -> bool:
        with self.edit_lock:
            w = self.find(key)
            if not w:
                return False
            w.s.enabled = False
            w.removed.set()
            if w.live:
                w.go_offline()
            self.watchers.remove(w)
            self.cfg.streamers = [s for s in self.cfg.streamers if s.key != w.s.key]
            self.cfg.save()
        return True

    def record_now(self, key: str) -> bool:
        w = self.find(key)
        if not w or not w.s.enabled:
            return False
        w.force = True
        w.hold_off = False
        w.wake.set()  # re-probe immediately
        return True

    def stop_recording(self, key: str) -> bool:
        w = self.find(key)
        if not (w and w.live):
            return False
        w.force = False
        w.hold_off = True
        w.go_offline()
        w.status = "live (stopped by you)"
        w.offline_since = time.time()
        return True

    def toggle_streamer(self, key: str) -> bool | None:
        w = self.find(key)
        if not w:
            return None
        w.s.enabled = not w.s.enabled
        self.cfg.save()
        return w.s.enabled

    def manual(self, key: str, seconds: float | None = None) -> bool:
        w = self.find(key)
        if not w:
            return False
        if seconds:
            now = time.time()
            return w.fire("manual", f"last {int(seconds)}s from dashboard", now - seconds, now + 3, auto=False)
        return w.fire_window("manual", "clipped from dashboard", auto=False)

    def stop(self) -> None:
        self.stopping.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
        for w in self.watchers:
            if w.chat:
                w.chat.stop()
            w._release_recorder(force=True)
