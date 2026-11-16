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
_local_lock = threading.Lock()


def extract_audio(video: Path, out: Path, start: float | None = None, dur: float | None = None) -> Path:
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.2f}"]
    cmd += ["-i", str(video)]
    if dur is not None:
        cmd += ["-t", f"{dur:.2f}"]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "48k", str(out)]
    subprocess.run(cmd, check=True)
    return out


def transcribe(cfg, media: Path) -> dict:
    """Return {"text": str, "words": [{"word", "start", "end"}]}. Never raises; empty on failure."""
    w = cfg["whisper"]
    backend = w.get("backend", "auto")
    with tempfile.TemporaryDirectory() as td:
        audio = extract_audio(media, Path(td) / "a.mp3")
        if backend in ("auto", "groq") and os.environ.get("GROQ_API_KEY"):
            try:
                return _groq(audio, w)
            except Exception as e:
                log.warning("Groq whisper failed (%s), using local", str(e)[:120])
        if backend == "groq" and not os.environ.get("GROQ_API_KEY"):
            log.warning("whisper.backend=groq but no GROQ_API_KEY, using local")
        try:
            return _local(audio, w)
        except Exception as e:
            log.error("local whisper failed: %s", e)
            return {"text": "", "words": []}


def _groq(audio: Path, w: dict) -> dict:
    data = {"model": w.get("groq_model", "whisper-large-v3-turbo"), "response_format": "verbose_json",
            "timestamp_granularities[]": ["word", "segment"]}
    if w.get("language"):
        data["language"] = w["language"]
    with open(audio, "rb") as f:
        r = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
            data=data, files={"file": ("a.mp3", f, "audio/mpeg")}, timeout=90,
        )
    r.raise_for_status()
    j = r.json()
    words = [{"word": x["word"].strip(), "start": float(x["start"]), "end": float(x["end"])} for x in j.get("words") or []]
    return {"text": j.get("text", "").strip(), "words": words}


def _local(audio: Path, w: dict) -> dict:
    global _local_model
    from faster_whisper import WhisperModel

    with _local_lock:
        if _local_model is None:
            name = w.get("local_model", "small")
            log.info("loading local whisper '%s' (first run downloads it)", name)
            _local_model = WhisperModel(name, device="auto", compute_type="int8")
        segs, _ = _local_model.transcribe(
            _pcm(audio), word_timestamps=True, vad_filter=True, language=w.get("language") or None, beam_size=1
        )
        words, text = [], []
        for s in segs:
            text.append(s.text.strip())
            for x in s.words or []:
                words.append({"word": x.word.strip(), "start": float(x.start), "end": float(x.end)})
    return {"text": " ".join(text), "words": words}


def _pcm(audio: Path):
    """Decode to 16 kHz mono float32 with ffmpeg (avoids PyAV version clashes in faster-whisper)."""
    import numpy as np

    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(audio), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
