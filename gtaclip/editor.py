"""Turn a raw clip into a finished post: Whisper captions, AI title/hook/hashtags, ffmpeg vertical edit."""
from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

from .captions import build_ass
from .config import FONTS_DIR
from .llm import LLM, clip_metadata
from .resources import encode_slot
from .transcribe import transcribe

log = logging.getLogger("edit")
REPO_MARK = "github.com/byte4day/gta6-clipper"


def probe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return float(out or 0)


def _esc(p: Path) -> str:
    # escape a path for use inside an ffmpeg filter argument
    return str(p).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")


def render(cfg, raw: Path, ass: Path, out: Path, vertical: bool, duration: float) -> None:
    e = cfg["edit"]
    has_audio = bool(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                                     "-of", "csv=p=0", str(raw)], capture_output=True, text=True).stdout.strip())
    if vertical:
        z = float(e.get("vertical_zoom", 1.18))
        # Blur a tiny frame then upscale — same look as full-res boxblur, ~5–10× cheaper on CPU/RAM.
        vf = (
            "[0:v]split[a][b];"
            "[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
            "gblur=sigma=12,scale=1080:1920,eq=brightness=-0.18:saturation=1.25[bg];"
            f"[b]crop=iw/{z}:ih:(iw-iw/{z})/2:0,scale=1080:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2-60[v0]"
        )
        W, H = 1080, 1920
    else:
        vf = "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2[v0]"
        W, H = 1920, 1080
    last = "v0"
    if e.get("progress_bar", True) and duration > 0:
        vf += (f";color=c=0xFF3EA5:s={W}x10:r=30[bar];"
               f"[{last}][bar]overlay=x='-W+W*t/{duration:.3f}':y=H-10:shortest=1[v1]")
        last = "v1"
    vf += f";[{last}]ass='{_esc(ass)}':fontsdir='{_esc(FONTS_DIR)}',format=yuv420p[vout]"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), "-filter_complex", vf, "-map", "[vout]"]
    if has_audio:
        cmd += ["-map", "0:a:0"]
        if e.get("loudnorm", True):
            # dynaudnorm is real-time; EBU loudnorm re-reads the whole file and fights live recorders.
            cmd += ["-af", "dynaudnorm=f=75:g=15:p=0.9"]
        cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "2", "-r", "30",
            "-t", f"{duration:.2f}", "-movflags", "+faststart", str(out)]
    with encode_slot():
        subprocess.run(cmd, check=True)


def process_clip(cfg, llm: LLM, clip_dir: Path, info: dict) -> dict:
    """info: streamer, platform, trigger, reason, title, category, chat (list[str]). Writes meta.json."""
    raw = clip_dir / "raw.mp4"
    meta_path = clip_dir / "meta.json"
    meta = {**info, "status": "transcribing"}
    meta_path.write_text(json.dumps(meta, indent=2))

    duration = probe_duration(raw)
    tr = transcribe(cfg, raw)
    (clip_dir / "transcript.json").write_text(json.dumps(tr, indent=2))
    meta.update(status="writing", duration=round(duration, 2), transcript=tr["text"])
    meta_path.write_text(json.dumps(meta, indent=2))

    pkg = clip_metadata(llm, info["streamer"], f"{info['trigger']}: {info.get('reason', '')}", tr["text"],
                        info.get("chat", []), info.get("title", ""), info.get("category", ""))
    meta.update(pkg)
    meta["status"] = "rendering"
    meta_path.write_text(json.dumps(meta, indent=2))

    meta["style"] = cfg["edit"].get("caption_style", "gta")
    render_outputs(cfg, clip_dir, meta, tr, duration)
    log.info("✔ clip ready: %s — %s", clip_dir.relative_to(clip_dir.parent.parent), pkg.get("title"))
    return meta


def write_post(clip_dir: Path, meta: dict) -> None:
    (clip_dir / "post.txt").write_text(
        f"{meta.get('title', '')}\n\n{meta.get('caption', '')}\n\n{' '.join(meta.get('hashtags', []))}\n")


def render_outputs(cfg, clip_dir: Path, meta: dict, tr: dict, duration: float) -> None:
    """(Re)build captions + every output format + thumbnail from raw.mp4. Updates meta.json."""
    e = cfg["edit"]
    style = meta.get("style") or e.get("caption_style", "gta")
    raw = clip_dir / "raw.mp4"
    tag = ("twitch.tv/" if meta.get("platform") == "twitch" else "youtube.com/@") + meta["streamer"]
    outputs = {}
    for fmt in e.get("formats", ["vertical"]):
        vertical = fmt == "vertical"
        ass = clip_dir / f"captions_{fmt}.ass"
        ass.write_text(build_ass(tr["words"], duration, style, vertical, hook=meta.get("hook", ""),
                                 hook_seconds=float(e.get("hook_seconds", 3.5)), tag=tag,
                                 per=int(e.get("words_per_caption", 3)),
                                 watermark=e.get("watermark_text", REPO_MARK) if e.get("watermark", True) else ""))
        out = clip_dir / f"final_{fmt}.mp4"
        tmp = clip_dir / f".rendering_{fmt}.mp4"
        render(cfg, raw, ass, tmp, vertical, duration)
        tmp.replace(out)
        outputs[fmt] = out.name
    main = clip_dir / outputs[next(iter(outputs))]
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{duration / 2:.2f}", "-i", str(main),
                    "-frames:v", "1", "-update", "1", "-vf", "scale=360:-2:out_range=full,format=yuvj420p",
                    str(clip_dir / "thumb.jpg")])
    write_post(clip_dir, meta)
    meta.update(status="done", outputs=outputs, rendered=int(time.time()))
    (clip_dir / "meta.json").write_text(json.dumps(meta, indent=2))


def rerender(cfg, clip_dir: Path, style: str | None = None) -> dict:
    """Re-render an existing clip (new caption style / edited hook) without re-transcribing."""
    meta = json.loads((clip_dir / "meta.json").read_text())
    tr = json.loads((clip_dir / "transcript.json").read_text())
    if style:
        meta["style"] = style
    meta["status"] = "rendering"
    (clip_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    try:
        render_outputs(cfg, clip_dir, meta, tr, meta.get("duration") or probe_duration(clip_dir / "raw.mp4"))
    except Exception as ex:
        meta.update(status="done" if meta.get("outputs") else "error", error=str(ex)[:200])
        (clip_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        raise
    return meta
