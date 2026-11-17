"""Speech-to-text with word timestamps: Groq Whisper API (free tier) or local faster-whisper."""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path

import requests

log = logging.getLogger("whisper")

_local_model = None
_local_name = None
_local_lock = threading.Lock()
_tls = threading.local()
GROQ_MAX_BYTES = 24 * 1024 * 1024  # API limit is 25 MB; leave headroom
_GROQ_OK = {".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm", ".ogg", ".flac"}


def _http() -> requests.Session:
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        _tls.session = s
    return s


def extract_audio(video: Path, out: Path, start: float | None = None, dur: float | None = None) -> Path:
    """Extract mono 16 kHz audio. Prefer ogg/opus for uploads (small); wav for local decode."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.2f}"]
    cmd += ["-i", str(video)]
    if dur is not None:
        cmd += ["-t", f"{dur:.2f}"]
    suf = out.suffix.lower()
    if suf in (".ogg", ".opus"):
        cmd += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k", str(out)]
    elif suf == ".mp3":
        cmd += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k", str(out)]
    else:
        cmd += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)]
    subprocess.run(cmd, check=True)
    return out


def _normalize(result: dict, duration: float | None = None) -> dict:
    from .captions import sanitize_words

    words = sanitize_words(result.get("words") or [], duration)
    text = (result.get("text") or "").strip()
    if not text and words:
        text = " ".join(w["word"] for w in words)
    return {"text": text, "words": words}


def _media_duration(media: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(media)],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        d = float(out or 0)
        return d if d > 0 else None
    except (ValueError, subprocess.SubprocessError):
        return None


def transcribe(cfg, media: Path) -> dict:
    """Return {"text": str, "words": [{"word", "start", "end"}]}. Never raises; empty on failure."""
    w = cfg["whisper"]
    backend = w.get("backend", "auto")
    duration = _media_duration(media)
    use_groq = backend in ("auto", "groq") and bool(os.environ.get("GROQ_API_KEY"))
    if use_groq:
        try:
            return _normalize(_groq(media, w), duration)
        except Exception as e:
            log.warning("Groq whisper failed (%s), using local", str(e)[:120])
            if backend == "groq":
                pass
    elif backend == "groq":
        log.warning("whisper.backend=groq but no GROQ_API_KEY, using local")
    try:
        return _normalize(_local(media, w), duration)
    except Exception as e:
        log.error("local whisper failed: %s", e)
        return {"text": "", "words": []}


def _groq(media: Path, w: dict) -> dict:
    """Upload original media when small enough; otherwise a compact opus extract."""
    upload = media
    tmp_dir = None
    try:
        too_big = media.stat().st_size > GROQ_MAX_BYTES
        bad_type = media.suffix.lower() not in _GROQ_OK
        if too_big or bad_type:
            tmp_dir = tempfile.TemporaryDirectory()
            # opus ~24 kb/s → ~3 min still under the 25 MB cap; far smaller than wav
            upload = extract_audio(media, Path(tmp_dir.name) / "a.ogg")
            if upload.stat().st_size > GROQ_MAX_BYTES:
                raise ValueError(f"audio still too large for Groq ({upload.stat().st_size} bytes)")
        data = {
            "model": w.get("groq_model", "whisper-large-v3-turbo"),
            "response_format": "verbose_json",
            "timestamp_granularities[]": ["word", "segment"],
        }
        if w.get("language"):
            data["language"] = w["language"]
        mime = {
            ".wav": "audio/wav", ".ogg": "audio/ogg", ".opus": "audio/ogg",
            ".mp3": "audio/mpeg", ".mp4": "video/mp4", ".m4a": "audio/mp4",
            ".webm": "video/webm", ".flac": "audio/flac",
        }.get(upload.suffix.lower(), "application/octet-stream")
        with open(upload, "rb") as f:
            r = _http().post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
                data=data,
                files={"file": (upload.name, f, mime)},
                timeout=90,
            )
        r.raise_for_status()
        j = r.json()
        words = [{"word": x["word"].strip(), "start": float(x["start"]), "end": float(x["end"])}
                 for x in j.get("words") or []]
        return {"text": j.get("text", "").strip(), "words": words}
    finally:
        if tmp_dir is not None:
            tmp_dir.cleanup()


def _local(media: Path, w: dict) -> dict:
    global _local_model, _local_name
    from faster_whisper import WhisperModel

    name = w.get("local_model", "base")
    # One ffmpeg pipe → float32; no intermediate wav on disk
    pcm = _pcm(media)
    try:
        with _local_lock:
            if _local_model is None or _local_name != name:
                log.info("loading local whisper '%s' (first run downloads it)", name)
                device, ctype = "cpu", "int8"
                try:
                    import ctranslate2
                    if ctranslate2.get_cuda_device_count() > 0:
                        device, ctype = "cuda", "int8_float16"
                except Exception:
                    pass
                _local_model = WhisperModel(name, device=device, compute_type=ctype)
                _local_name = name
            model = _local_model
        # Transcribe outside the lock so parallel clips / AI scans don't queue on each other
        segs, _ = model.transcribe(
            pcm,
            word_timestamps=True,
            vad_filter=True,
            language=w.get("language") or None,
            beam_size=1,
            best_of=1,
            temperature=0.0,
            condition_on_previous_text=False,
            vad_parameters={"min_silence_duration_ms": 400},
        )
        words, text = [], []
        for s in segs:
            text.append(s.text.strip())
            for x in s.words or []:
                words.append({"word": x.word.strip(), "start": float(x.start), "end": float(x.end)})
        return {"text": " ".join(text), "words": words}
    finally:
        del pcm  # drop the float32 buffer ASAP (AI scans + clips share this process)


def _pcm(media: Path):
    """Decode any media to 16 kHz mono float32 with one ffmpeg pass."""
    import numpy as np

    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(media),
         "-vn", "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
