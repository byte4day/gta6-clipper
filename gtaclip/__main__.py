"""CLI: python -m gtaclip run | clip-file | test-ai"""
from __future__ import annotations

import argparse
import os
import json
import logging
import shutil
import signal
import threading
from datetime import datetime
from pathlib import Path

from .config import load_config


def main() -> None:
    ap = argparse.ArgumentParser(prog="gtaclip", description="Real-time GTA 6 stream clipper")
    ap.add_argument("--config", help="path to config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="watch streamers, clip live")
    r.add_argument("--only", nargs="*", help="only these streamers (names from config or any twitch login)")
    r.add_argument("--no-web", action="store_true")
    f = sub.add_parser("clip-file", help="edit an existing video file into a captioned clip (test the editor)")
    f.add_argument("video")
    f.add_argument("--streamer", default="streamer")
    f.add_argument("--style", help="gta | bold | mrbeast | minimal")
    f.add_argument("--start", type=float, default=0)
    f.add_argument("--duration", type=float, default=30)
    sub.add_parser("test-ai", help="check which LLM/Whisper providers work")
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-5s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("faster_whisper", "urllib3", "chat_downloader"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = load_config(a.config)

    if a.cmd == "run":
        from .config import Streamer
        from .monitor import App
        from .web import serve

        if a.only:  # allow ad-hoc twitch logins not in config
            known = {s.key for s in cfg.streamers}
            cfg.streamers += [Streamer("youtube" if o.startswith("@") else "twitch", o) for o in a.only if o.lstrip("@").lower() not in known]
        app = App(cfg, a.only)
        if not app.watchers:
            raise SystemExit("no enabled streamers in config.toml")
        logging.info("watching %d streamers · AI providers: %s", len(app.watchers), ", ".join(app.llm.available()) or "none (add keys to .env)")
        app.start()
        if not a.no_web:
            port = int(cfg["general"].get("dashboard_port", 8787))
            srv = serve(app, port)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            logging.info("dashboard → http://127.0.0.1:%d", port)
        done = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: done.set())
        signal.signal(signal.SIGTERM, lambda *_: done.set())
        done.wait()
        logging.info("stopping…")
        app.stop()
        logging.shutdown()
        os._exit(0)  # don't wait on half-finished clip renders or library threads

    elif a.cmd == "clip-file":
        import subprocess

        from .editor import process_clip
        from .llm import LLM

        if a.style:
            cfg.raw.setdefault("edit", {})["caption_style"] = a.style
        d = cfg.path("output_dir") / a.streamer.lower() / f"{datetime.now():%Y%m%d-%H%M%S}_manual"
        d.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(a.start), "-i", a.video, "-t", str(a.duration),
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", str(d / "raw.mp4")], check=True)
        meta = process_clip(cfg, LLM(cfg), d, {"streamer": a.streamer, "platform": "twitch", "trigger": "manual",
                                               "reason": "file", "title": Path(a.video).stem, "category": "Grand Theft Auto VI",
                                               "chat": [], "started": datetime.now().isoformat(timespec="seconds")})
        print(json.dumps({k: meta.get(k) for k in ("title", "hook", "caption", "hashtags", "outputs")}, indent=2))
        print(f"→ {d}")

    elif a.cmd == "test-ai":

        from .llm import LLM, PROVIDERS

        llm = LLM(cfg)
        for p in llm.order:
            if not os.environ.get(PROVIDERS[p][1]):
                print(f"  {p:<11} – no {PROVIDERS[p][1]}")
                continue
            one = LLM(cfg)
            one.order = [p]
            out = one.chat("Reply with exactly: ok", "ping", max_tokens=5)
            print(f"  {p:<11} {'✔ ' + out.strip()[:20] if out else '✗ failed'}")
        print("  whisper     " + ("groq API" if os.environ.get("GROQ_API_KEY") else f"local faster-whisper ({cfg['whisper'].get('local_model')})"))
        print("  ffmpeg      " + ("✔" if shutil.which("ffmpeg") else "✗ missing"))


if __name__ == "__main__":
    main()
