"""Rolling live recording + clip cutting while the stream is still live.

Backends
  browser   : headless Chrome opens the channel, we capture the player's HLS playlist and pull
              segments ourselves (Twitch ad segments are skipped). Default for Twitch.
  streamlink: streamlink --stdout. Used for YouTube (its web player has no HLS) and as fallback.
Both feed one ffmpeg that writes numbered 4 s .ts segments into buffer/<streamer>/.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

from . import core
from .browser import UA, Browser
from .resources import encode_slot, encoding

log = logging.getLogger("rec")

JPEG_VF = "scale={w}:-2:out_range=full,format=yuvj420p"
STREAMLINK = shutil.which("streamlink") or str(Path(__file__).resolve().parent.parent / ".venv/bin/streamlink")


def parse_master(text: str, base: str) -> list[dict]:
    """Variants from an HLS master playlist: [{name, bandwidth, height, url}]."""
    out, attrs = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = dict(re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line.split(":", 1)[1]))
            attrs = {k: v.strip('"') for k, v in attrs.items()}
        elif line and not line.startswith("#") and attrs is not None:
            res = attrs.get("RESOLUTION", "0x0")
            h = int(res.split("x")[-1]) if "x" in res else 0
            fps = float(attrs.get("FRAME-RATE", 30) or 30)
            name = attrs.get("VIDEO") or (f"{h}p{60 if fps > 40 else ''}" if h else "audio_only")
            out.append({"name": name, "bandwidth": int(attrs.get("BANDWIDTH", 0)), "height": h, "url": urljoin(base, line)})
            attrs = None
    return out


def pick_variant(variants: list[dict], quality: str) -> dict | None:
    video = [v for v in variants if v["height"] and "audio" not in v["name"]]
    if not video:
        return variants[0] if variants else None
    for q in [q.strip() for q in quality.split(",")]:
        if q == "best":
            return max(video, key=lambda v: v["bandwidth"])
        if q == "worst":
            return min(video, key=lambda v: v["bandwidth"])
        for v in video:
            if v["name"].lower().startswith(q.lower()) and (q.endswith("60") or "60" not in v["name"] or q == v["name"]):
                return v
        for v in video:  # "720p" also accepts "720p60"
            if v["name"].lower().startswith(q.lower()):
                return v
    return max(video, key=lambda v: v["bandwidth"])


class HLSPuller(threading.Thread):
    """Polls a live media playlist and writes new segments to ffmpeg's stdin."""

    def __init__(self, rec: "Recorder", master_url: str):
        super().__init__(daemon=True, name=f"hls-{rec.streamer.key}")
        self.rec = rec
        self.master = master_url
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": UA, "Origin": "https://www.twitch.tv", "Referer": "https://www.twitch.tv/"})
        self.variant = ""
        self.done = False
        self.reason = ""

    def _media_url(self) -> str | None:
        r = self.http.get(self.master, timeout=15)
        if r.status_code != 200:
            return None
        v = pick_variant(parse_master(r.text, self.master), self.rec.quality)
        if v:
            self.variant = v["name"]
            self.rec.quality_name = v["name"]
        return v["url"] if v else None

    def run(self) -> None:
        try:
            self._loop()
        except Exception as e:
            self.reason = f"{type(e).__name__}: {str(e)[:120]}"
            log.warning("hls %s: %s", self.rec.streamer.display, self.reason)
        finally:
            self.done = True
            try:
                self.rec.proc_ff.stdin.close()
            except Exception:
                pass

    def _loop(self) -> None:
        media = self._media_url()
        if not media:
            self.reason = "no media playlist"
            return
        last_seq, fails, reresolved = -1, 0, 0
        written_map = None  # fMP4 init segment (#EXT-X-MAP) currently feeding ffmpeg
        while not self.rec._stop.is_set():
            t = time.time()
            try:
                r = self.http.get(media, timeout=10)
            except requests.RequestException:
                r = None
            if r is None or r.status_code != 200:
                fails += 1
                if fails in (4, 8) and reresolved < 3:  # token expired? ask the browser again
                    reresolved += 1
                    fresh = Browser.get().resolve(self.rec.streamer.url)
                    if fresh:
                        self.master = fresh
                        media = self._media_url() or media
                        fails = 0
                elif fails > 10:
                    self.reason = "playlist unreachable"
                    return  # stream is gone
                time.sleep(2)
                continue
            fails = 0
            text = r.text
            seq0 = int((re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", text) or [0, 0])[1])
            target = float((re.search(r"#EXT-X-TARGETDURATION:(\d+)", text) or [0, 2])[1])
            seq, title, in_ad, cur_map = seq0, None, False, None
            if last_seq < 0:  # first load: start at the live edge (newest 3 segments)
                last_seq = seq0 + text.count("#EXTINF") - 4
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("#EXT-X-MAP:"):
                    m = re.search(r'URI="([^"]+)"', line)
                    cur_map = urljoin(media, m.group(1)) if m else None
                elif line.startswith("#EXTINF:"):
                    title = line.split(",", 1)[1] if "," in line else ""
                elif line and not line.startswith("#"):
                    if seq > last_seq:
                        ad = self.rec.streamer.platform == "twitch" and title not in (None, "", "live")
                        if ad:
                            in_ad = True
                        else:
                            data = self.http.get(urljoin(media, line), timeout=15).content
                            try:
                                if cur_map and cur_map != written_map:
                                    if written_map is not None:  # format changed: new ffmpeg, same buffer
                                        self.rec.restart_ffmpeg()
                                    init = self.http.get(cur_map, timeout=15).content
                                    self.rec.proc_ff.stdin.write(init)
                                    written_map = cur_map
                                self.rec.proc_ff.stdin.write(data)
                                self.rec.proc_ff.stdin.flush()
                            except (BrokenPipeError, OSError) as e:
                                self.reason = f"ffmpeg pipe closed: {e}"
                                return
                            self.rec.bytes_in += len(data)
                        last_seq = seq
                    seq += 1
                    title = None
            self.rec.in_ad = in_ad
            if "#EXT-X-ENDLIST" in text:
                self.reason = "stream ended (ENDLIST)"
                return
            time.sleep(max(0.5, target / 2 - (time.time() - t)))


class Recorder:
    def __init__(self, streamer, cfg, on_loudness=None):
        g = cfg["general"]
        self.streamer = streamer
        self.dir = cfg.path("buffer_dir") / streamer.key
        self.seg = int(g.get("segment_seconds", 4))
        self.keep = float(g.get("buffer_minutes", 6)) * 60
        self.quality = g.get("quality", "best")
        self.backend_pref = g.get("recorder", "browser")
        self.on_loudness = on_loudness
        self.holds: dict[int, float] = {}  # job id -> earliest time that must stay on disk
        self.proc_sl = self.proc_ff = None
        self.puller: HLSPuller | None = None
        self.backend = ""
        self.quality_name = ""
        self.in_ad = False
        self.bytes_in = 0
        self.started_at = 0.0
        self._stop = threading.Event()
        self._analyzed: set[str] = set()
        self._ends: dict[int, float] = {}  # segment index -> wall-clock end time
        self._last_preview = 0.0
        self._durs: dict[str, float] = {}  # segment name -> real media duration

    # ---- process control -------------------------------------------------
    def _close_fflog(self) -> None:
        logf = getattr(self, "_fflog", None)
        if logf and not logf.closed:
            try:
                logf.close()
            except OSError:
                pass
        self._fflog = None

    def _ffmpeg(self, stdin, start_number: int = 0) -> subprocess.Popen:
        ff = ["ffmpeg", "-loglevel", "error", "-fflags", "+genpts+discardcorrupt", "-i", "pipe:0",
              "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-f", "segment", "-segment_time", str(self.seg),
              "-segment_start_number", str(start_number), "-segment_format", "mpegts", str(self.dir / "%06d.ts")]
        self._close_fflog()
        self._fflog = open(self.dir / "ffmpeg.log", "ab")
        # Recording stays ahead of clip encodes: slightly nicer than default, never below normal for puller.
        return subprocess.Popen(ff, stdin=stdin, stderr=self._fflog)

    def restart_ffmpeg(self) -> None:
        old = self.proc_ff
        try:
            if old and old.stdin:
                old.stdin.close()
            if old:
                old.wait(10)
        except Exception:
            if old:
                old.kill()
        files = self._files()
        self.proc_ff = self._ffmpeg(subprocess.PIPE, (files[-1][0] + 1) if files else 0)

    def start(self) -> None:
        if self.dir.exists():
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True)
        self._stop.clear()
        master = None
        if self.backend_pref in ("browser", "auto") and self.streamer.platform == "twitch":
            master = Browser.get().resolve(self.streamer.url)
            if not master:
                log.warning("[%s] browser couldn't get the stream, falling back to streamlink", self.streamer.display)
        if master:
            self.backend = "browser"
            self.proc_ff = self._ffmpeg(subprocess.PIPE)
            self.puller = HLSPuller(self, master)
            self.puller.start()
        else:
            self.backend = "streamlink"
            sl = [STREAMLINK, "--stdout", "--loglevel", "warning", "--hls-live-edge", "2",
                  "--stream-segment-threads", "2", self.streamer.url, self.quality]
            if self.streamer.platform == "twitch":
                sl[1:1] = ["--twitch-disable-ads"]
            self.proc_sl = subprocess.Popen(sl, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.proc_ff = self._ffmpeg(self.proc_sl.stdout)
            self.proc_sl.stdout.close()
            self.quality_name = ""
        self.started_at = time.time()
        threading.Thread(target=self._housekeep, daemon=True, name=f"rec-{self.streamer.key}").start()
        log.info("● recording %s via %s", self.streamer.display, self.backend)

    def alive(self) -> bool:
        if not (self.proc_ff and self.proc_ff.poll() is None):
            return False
        if self.backend == "browser":
            return bool(self.puller and not self.puller.done)
        return bool(self.proc_sl and self.proc_sl.poll() is None)

    def why_dead(self) -> str:
        if self.proc_ff and self.proc_ff.poll() is not None:
            return f"ffmpeg exited {self.proc_ff.returncode}"
        if self.backend == "browser" and self.puller and self.puller.done:
            return f"playlist puller stopped: {self.puller.reason or 'unknown'}"
        if self.proc_sl and self.proc_sl.poll() is not None:
            return f"streamlink exited {self.proc_sl.returncode}"
        return "recorder stopped"

    def stop(self) -> None:
        self._stop.set()
        if self.proc_ff and self.proc_ff.stdin and self.backend == "browser":
            try:
                self.proc_ff.stdin.close()
            except OSError:
                pass
        for p in (self.proc_sl, self.proc_ff):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
        self._close_fflog()
        if self.puller:
            try:
                self.puller.http.close()
            except Exception:
                pass

    def cleanup(self) -> None:
        """Stop ingest and delete the ring buffer (call only when no clip still needs it)."""
        self.stop()
        self._analyzed.clear()
        self._durs.clear()
        self._ends.clear()
        self.holds.clear()
        if self.dir.exists():
            shutil.rmtree(self.dir, ignore_errors=True)

    # ---- buffer ----------------------------------------------------------
    def _files(self) -> list[tuple[int, Path]]:
        try:
            return sorted((int(p.stem), p) for p in self.dir.glob("*.ts") if p.stem.isdigit())
        except FileNotFoundError:
            return []

    def _forget_seg(self, name: str, idx: int | None = None) -> None:
        self._analyzed.discard(name)
        self._durs.pop(name, None)
        if idx is not None:
            self._ends.pop(idx, None)

    def segments(self) -> list[tuple[float, float, Path]]:
        """Completed segments as (timeline_start, timeline_end, path).

        Timeline is anchored to the newest segment's mtime, then walked backwards using
        real media durations when known — so "last 30s" matches actual audio/video, not
        wall-clock gaps from ads/stalls.
        """
        files = self._files()
        completed = files[:-1]  # newest file is still being written
        if not completed:
            return []
        rows: list[tuple[int, Path, float]] = []
        for i, p in completed:
            try:
                end_mtime = self._ends.get(i)
                if end_mtime is None:
                    end_mtime = self._ends[i] = p.stat().st_mtime
            except FileNotFoundError:
                continue
            dur = self._durs.get(p.name) or 0.0
            if dur <= 0.05:
                dur = float(self.seg)
            rows.append((i, p, dur))
        if not rows:
            return []
        # Anchor the last completed segment's end to when it landed on disk
        last_i, last_p, _ = rows[-1]
        t = self._ends.get(last_i, last_p.stat().st_mtime)
        out_rev: list[tuple[float, float, Path]] = []
        for i, p, dur in reversed(rows):
            start = t - dur
            out_rev.append((start, t, p))
            t = start
        out_rev.reverse()
        return out_rev

    def buffered_until(self) -> float:
        segs = self.segments()
        return segs[-1][1] if segs else 0.0

    def buffered_from(self) -> float:
        segs = self.segments()
        return segs[0][0] if segs else 0.0

    def live_playlist(self, n: int = 8) -> str:
        """Rolling HLS playlist of the buffer (for the dashboard's live preview player)."""
        files = self._files()[:-1][-n:]
        if not files:
            return ""
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{self.seg * 2}", f"#EXT-X-MEDIA-SEQUENCE:{files[0][0]}"]
        for i, p in files:
            dur = self._durs.get(p.name) or self.seg
            lines += [f"#EXTINF:{dur:.3f},", f"seg/{p.name}"]
        return "\n".join(lines) + "\n"

    def cut(self, t0: float, t1: float, out: Path, accurate: bool = True) -> bool:
        """Cut [t0, t1] from the ring buffer into out.

        accurate=True (default): frame-accurate re-encode so A/V start at 0 — required for
        captions. accurate=False: try stream-copy (fine for throwaway AI-scan audio).
        """
        segs = [s for s in self.segments() if s[1] > t0 and s[0] < t1]
        if not segs:
            return False
        offset = max(0.0, t0 - segs[0][0])
        dur = min(t1, segs[-1][1]) - max(t0, segs[0][0])
        if dur <= 0.05:
            return False
        with tempfile.TemporaryDirectory() as td:
            lst = Path(td) / "list.txt"
            # Absolute paths: concat resolves relative entries against the list file's directory.
            lines = []
            for _, _, p in segs:
                ap = str(p.resolve()).replace("'", r"'\''")
                lines.append(f"file '{ap}'\n")
            lst.write_text("".join(lines))
            if not accurate:
                copy = subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                     "-ss", f"{offset:.3f}", "-t", f"{dur:.3f}", "-c", "copy", "-bsf:a", "aac_adtstoasc",
                     "-movflags", "+faststart", str(out)],
                    capture_output=True)
                if copy.returncode == 0 and out.exists() and out.stat().st_size > 10000:
                    return True
                out.unlink(missing_ok=True)
            # Re-encode: keeps video/audio locked and starting at pts 0 (stream-copy was ~2s early on video).
            with encode_slot():
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                     "-ss", f"{offset:.3f}", "-t", f"{dur:.3f}",
                     "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-threads", "2",
                     "-fps_mode", "cfr", "-r", "30",
                     "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
                     "-af", "aresample=async=1:first_pts=0",
                     "-movflags", "+faststart", str(out)],
                    check=True)
        return out.exists() and out.stat().st_size > 10000

    def preview(self) -> Path:
        return self.dir / "preview.jpg"

    def _make_preview(self, seg: Path) -> None:
        tmp = self.dir / "preview.tmp.jpg"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(seg), "-frames:v", "1", "-update", "1",
                        "-vf", JPEG_VF.format(w=640), "-q:v", "5", str(tmp)], timeout=20)
        if tmp.exists():
            os.replace(tmp, self.preview())

    def _housekeep(self) -> None:
        while not self._stop.wait(1.0):
            segs = self.segments()
            if not segs:
                continue
            # prune old segments, but never ones a pending clip still needs
            floor = min([time.time() - self.keep, *list(self.holds.values())])
            for a, b, p in segs:
                if b < floor:
                    idx = int(p.stem) if p.stem.isdigit() else None
                    p.unlink(missing_ok=True)
                    self._forget_seg(p.name, idx)
            busy = encoding()  # while a clip encodes, skip extra ffmpeg so live pullers keep CPU/IO
            try:
                if not busy and time.time() - self._last_preview > 4:
                    self._last_preview = time.time()
                    self._make_preview(segs[-1][2])
            except (subprocess.SubprocessError, OSError):
                pass
            # duration + loudness of each new segment (loudness = scream / hype detection)
            for a, b, p in segs[-3:]:
                if p.name in self._analyzed:
                    continue
                self._analyzed.add(p.name)
                self._durs[p.name] = media_duration(p)
                if busy or not self.on_loudness:
                    continue
                peak = momentary_peak(p)
                if peak is not None:
                    self.on_loudness(a, b, peak)


def momentary_peak(path: Path) -> float | None:
    """Max EBU R128 momentary loudness (LUFS) of a segment."""
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "ebur128", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return None
    vals = [float(m) for m in re.findall(r"\bM:\s*(-?\d+\.\d+)", r.stderr)]
    vals = [v for v in vals if v > -70]
    return max(vals) if vals else None


def media_duration(path: Path) -> float:
    fast = core.ts_duration(path) if path.suffix == ".ts" else None
    if fast:
        return fast
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=15).stdout.strip()
        return float(out)
    except (ValueError, subprocess.SubprocessError):
        return 0.0
