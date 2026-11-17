"""Local dashboard: live status, clip feed, manual 'clip now'."""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import shutil
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import transcribe
from .avatars import avatar_path
from . import core
from .config import FONTS_DIR, set_env_key
from .captions import STYLES
from .editor import _eta_seconds, rerender, write_post
from .llm import LLM, PROVIDERS
from . import socials


def _safe_rerender(app, d, style):
    try:
        rerender(app.cfg, d, style)
    except Exception as e:
        logging.getLogger("edit").warning("re-render %s failed: %s", d.name, e)

HTML = Path(__file__).with_name("dashboard.html")


def list_clips(out: Path, limit: int = 120) -> list[dict]:
    metas = sorted(out.glob("*/*/meta.json"), key=lambda p: p.parent.name, reverse=True)[:limit]
    clips = []
    for m in metas:
        try:
            d = json.loads(m.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        d["chat"] = (d.get("chat") or [])[-25:]
        d["dir"] = m.parent.relative_to(out).as_posix()
        clips.append(d)
    return clips


KEY_PAGES = {
    "groq": "https://console.groq.com/keys",
    "openrouter": "https://openrouter.ai/keys",
    "cerebras": "https://cloud.cerebras.ai",
    "gemini": "https://aistudio.google.com/apikey",
    "mistral": "https://console.mistral.ai/api-keys",
}

# settings the dashboard may change: section -> key -> (type, allowed values or None)
EDITABLE = {
    "edit": {"caption_style": (str, ["gta", "bold", "mrbeast", "minimal"]), "words_per_caption": (int, None),
             "formats": (list, ["vertical", "landscape"]), "vertical_zoom": (float, None),
             "watermark": (bool, None), "progress_bar": (bool, None)},
    "general": {"record_mode": (str, ["manual", "always", "gta_only"]), "max_recordings": (int, None),
                "recorder": (str, ["browser", "streamlink"]), "quality": (str, ["1080p60,best", "720p60,720p,best", "480p,best", "best"])},
    "llm": {"ai_scan": (bool, None), "ai_min_score": (int, None), "scan_seconds": (int, None)},
    "whisper": {"backend": (str, ["auto", "groq", "local"]), "local_model": (str, ["tiny", "base", "small", "medium", "large-v3"]),
                "language": (str, None)},
    "detect": {"chat_spike_ratio": (float, None), "audio_spike_db": (float, None), "cooldown_seconds": (int, None),
               "first_moment_seconds": (int, None)},
    "socials": {"auto_upload": (bool, None), "platforms": (list, list(socials.PLATFORMS)),
                "privacy": (str, ["public", "unlisted", "private"]),
                "tiktok_privacy": (str, ["private", "public", "friends", "SELF_ONLY",
                                         "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS"])},
}


def mask(v: str) -> str:
    return (v[:4] + "…" + v[-4:]) if len(v) > 12 else "set"


def settings_payload(app) -> dict:
    cfg = app.cfg
    models = cfg["llm"].get("models", {})
    providers = []
    for p in app.llm.order:
        env = PROVIDERS[p][1]
        val = os.environ.get(env, "")
        providers.append({"id": p, "env": env, "has_key": bool(val), "masked": mask(val) if val else "",
                          "model": models.get(p, ""), "get_key": KEY_PAGES.get(p, "")})
    current = {sec: {k: cfg[sec].get(k) for k in keys} for sec, keys in EDITABLE.items()}
    choices = {sec: {k: v[1] for k, v in keys.items() if v[1]} for sec, keys in EDITABLE.items()}
    port = int(cfg["general"].get("dashboard_port", 8787))
    usage = app.llm.usage_snapshot()
    wh = transcribe.usage_snapshot()
    if wh.get("calls") or wh.get("errors"):
        usage["whisper"] = {
            "calls": wh.get("calls", 0), "errors": wh.get("errors", 0),
            "prompt_tokens": 0, "completion_tokens": 0, "last": wh.get("last"),
            "backend": wh.get("last_backend") or "",
        }
    return {"providers": providers, "settings": current, "choices": choices,
            "streamers": [{"platform": s.platform, "name": s.name, "key": s.key, "enabled": s.enabled} for s in cfg.streamers],
            "socials": socials.status_payload(cfg, port),
            "usage": usage,
            "clip_eta": {str(s): _eta_seconds(s) for s in (15, 30, 60, 90)}}


def coerce(typ, allowed, v):
    if typ is bool:
        v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
    elif typ is list:
        v = [x for x in v if not allowed or x in allowed] if isinstance(v, list) else None
        if not v:
            raise ValueError("pick at least one")
        return v
    else:
        v = typ(v)
    if allowed and v not in allowed:
        raise ValueError(f"must be one of {allowed}")
    return v


def make_handler(app):
    out = app.out.resolve()
    port = int(app.cfg["general"].get("dashboard_port", 8787))
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _file(self, path: Path, root: Path, cache: bool = True):
            path = path.resolve()
            if not path.is_relative_to(root) or not path.is_file():
                return self._json({"error": "not found"}, 404)
            size = path.stat().st_size
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            start, end = 0, size - 1
            m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else end
                else:
                    start = size - int(m.group(2))
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            if not cache:
                self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 16, left))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    left -= len(chunk)

        def _host_ok(self) -> bool:
            # blocks DNS-rebinding: only answer requests addressed to this machine
            return self.headers.get("Host", "") in hosts

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 100_000:
                raise ValueError("body too large")
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _guard(self) -> bool:
            # writes need a custom header, which a foreign website cannot send without a CORS preflight we never allow
            if not self._host_ok() or self.headers.get("X-Clipper") != "1":
                self._json({"error": "forbidden"}, 403)
                return False
            return True

        def do_GET(self):
            p = unquote(urlparse(self.path).path)
            if p.startswith("/api/") and not self._host_ok():
                return self._json({"error": "forbidden"}, 403)
            if p == "/":
                body = HTML.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif p == "/api/status":
                self._json({"streamers": [w.snapshot() for w in app.watchers],
                            "llm": app.llm.available(), "recording": app.recording_count(),
                            "core": core.available()})
            elif p == "/api/clips":
                self._json(list_clips(out))
            elif p.startswith("/files/"):
                self._file(out / p[len("/files/"):], out)
            elif p == "/api/settings":
                self._json(settings_payload(app))
            elif p == "/api/socials":
                self._json(socials.status_payload(app.cfg, port))
            elif m := re.fullmatch(r"/api/socials/callback/(youtube|tiktok|instagram)", p):
                qs = parse_qs(urlparse(self.path).query)
                err = (qs.get("error_description") or qs.get("error") or [None])[0]
                try:
                    if err:
                        raise ValueError(err)
                    code = (qs.get("code") or [""])[0]
                    state = (qs.get("state") or [""])[0]
                    if not code or not state:
                        raise ValueError("missing OAuth code")
                    account = socials.finish_oauth(m.group(1), code, state, port)
                    body = socials.connected_html(account)
                except Exception as e:
                    body = socials.connected_html("", error=str(e)[:300])
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif m := re.fullmatch(r"/api/chat/([\w.@-]+)", p):
                w = app.find(m.group(1))
                since = float((parse_qs(urlparse(self.path).query).get("since") or [0])[0])
                msgs = [{"t": t, "u": u, "m": x} for t, u, x in (w.chat.msgs if w and w.chat else []) if t > since][-80:]
                self._json({"msgs": msgs, "rate": w.chat.rate(5) if w and w.chat else 0})
            elif m := re.fullmatch(r"/live/([\w.@-]+)/index\.m3u8", p):
                w = app.find(m.group(1))
                pl = w.rec.live_playlist() if w and w.rec and w.rec.alive() else ""
                if not pl:
                    return self._json({"error": "not recording"}, 404)
                body = pl.encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif m := re.fullmatch(r"/live/([\w.@-]+)/seg/(\d+\.ts)", p):
                w = app.find(m.group(1))
                if not (w and w.rec):
                    return self._json({"error": "not found"}, 404)
                self._file(w.rec.dir / m.group(2), w.rec.dir.resolve())
            elif m := re.fullmatch(r"/preview/([\w.@-]+)\.jpg", p):
                w = app.find(m.group(1))
                if not (w and w.rec):
                    return self._json({"error": "not found"}, 404)
                self._file(w.rec.preview(), w.rec.dir.resolve())
            elif p.startswith("/avatar/"):
                self._file(avatar_path(p[len("/avatar/"):]), avatar_path("x").parent.resolve())
            elif p in ("/logo.svg", "/logo.png"):
                self._file(FONTS_DIR.parent / p[1:], FONTS_DIR.parent.resolve())
            elif p.startswith("/fonts/"):
                self._file(FONTS_DIR / p[len("/fonts/"):], FONTS_DIR.resolve())
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            if not self._guard():
                return
            p = urlparse(self.path).path
            try:
                body = self._body()
                if m := re.fullmatch(r"/api/clip/([\w.@-]+)", p):
                    secs = float(body["seconds"]) if body.get("seconds") else None
                    if secs is not None and not 5 <= secs <= 180:
                        raise ValueError("seconds must be 5-180")
                    style = body.get("style")
                    if style is not None and style not in STYLES:
                        raise ValueError("style must be gta / bold / mrbeast / minimal")
                    ok = app.manual(m.group(1), secs, style=style)
                    label = f"last {int(secs)}s" if secs else "last 35s"
                    eta = int(_eta_seconds(secs or 35))
                    msg = f"✂ clipping {label} · ~{eta}s render" if ok else "not recording right now"
                    return self._json({"ok": ok, "msg": msg, "eta_s": eta if ok else None}, 200 if ok else 409)
                if m := re.fullmatch(r"/api/(record|stoprec)/([\w.@-]+)", p):
                    if m.group(1) == "record":
                        ok = app.record_now(m.group(2))
                        w = app.find(m.group(2))
                        if ok and w and w.live:
                            msg = "recording started"
                        elif ok:
                            msg = "selected — will record when they go live"
                        else:
                            msg = "streamer not found / paused"
                        return self._json({"ok": ok, "msg": msg})
                    ok = app.stop_recording(m.group(2))
                    return self._json({"ok": ok, "msg": "stopped" if ok else "not recording / not selected"})
                if m := re.fullmatch(r"/api/clips/([\w.@-]+/[\w.-]+)/(meta|rerender)", p):
                    d = (out / m.group(1)).resolve()
                    if not d.is_relative_to(out) or not (d / "meta.json").exists():
                        return self._json({"ok": False, "msg": "clip not found"}, 404)
                    meta = json.loads((d / "meta.json").read_text())
                    if meta.get("status") not in ("done", "error"):
                        return self._json({"ok": False, "msg": "clip is still being made"}, 409)
                    for k in ("title", "caption", "hook"):
                        if k in body:
                            meta[k] = str(body[k])[:500]
                    if "hashtags" in body:
                        tags = body["hashtags"] if isinstance(body["hashtags"], list) else str(body["hashtags"]).split()
                        meta["hashtags"] = ["#" + t.lstrip("#") for t in tags if t.strip("#")][:30]
                    (d / "meta.json").write_text(json.dumps(meta, indent=2))
                    write_post(d, meta)
                    if m.group(2) == "meta":
                        return self._json({"ok": True, "msg": "saved"})
                    style = body.get("style")
                    if style and style not in STYLES:
                        raise ValueError("unknown caption style")
                    app.pool.submit(_safe_rerender, app, d, style)
                    return self._json({"ok": True, "msg": "re-rendering… (~30 s)"})
                if p == "/api/keys":
                    prov = body.get("provider")
                    if prov not in PROVIDERS:
                        raise ValueError("unknown provider")
                    if "key" in body:
                        set_env_key(PROVIDERS[prov][1], str(body["key"]).strip())
                        app.llm.cooldown.pop(prov, None)
                    if body.get("model"):
                        app.cfg["llm"].setdefault("models", {})[prov] = str(body["model"]).strip()
                        app.cfg.set("llm", "models", app.cfg["llm"]["models"])
                    return self._json({"ok": True, "msg": f"{prov} saved"})
                if m := re.fullmatch(r"/api/test/(\w+)", p):
                    prov = m.group(1)
                    if prov not in PROVIDERS or not os.environ.get(PROVIDERS[prov][1]):
                        return self._json({"ok": False, "msg": f"add a {prov} key first"})
                    one = LLM(app.cfg)
                    one.order = [prov]
                    reply = one.chat("Reply with exactly: ok", "ping", max_tokens=5)
                    return self._json({"ok": bool(reply), "msg": f"{prov} works ✔" if reply else f"{prov} failed: check key / model"})
                if p == "/api/settings":
                    for sec, vals in body.items():
                        for k, v in vals.items():
                            if sec not in EDITABLE or k not in EDITABLE[sec]:
                                raise ValueError(f"{sec}.{k} can't be changed here")
                            typ, allowed = EDITABLE[sec][k]
                            app.cfg.set(sec, k, coerce(typ, allowed, v))
                            if sec == "whisper" and k == "local_model":
                                transcribe._local_model = None  # reload with the new size on next clip
                                transcribe._local_name = None
                    return self._json({"ok": True, "msg": "settings saved"})
                if p == "/api/socials/creds":
                    plat = body.get("platform")
                    if plat not in socials.PLATFORMS:
                        raise ValueError("unknown platform")
                    cid = body.get("client_id")
                    sec = body.get("client_secret")
                    socials.save_app_creds(plat,
                                          None if cid is None else str(cid),
                                          None if sec is None else str(sec))
                    return self._json({"ok": True, "msg": f"{plat} app saved"})
                if p == "/api/socials/connect":
                    plat = body.get("platform")
                    if plat not in socials.PLATFORMS:
                        raise ValueError("unknown platform")
                    url = socials.start_oauth(plat, port)
                    return self._json({"ok": True, "url": url, "msg": f"open the {plat} login window"})
                if m := re.fullmatch(r"/api/socials/(youtube|tiktok|instagram)/disconnect", p):
                    socials.disconnect(m.group(1))
                    return self._json({"ok": True, "msg": f"{m.group(1)} disconnected"})
                if m := re.fullmatch(r"/api/clips/([\w.@-]+/[\w.-]+)/upload", p):
                    d = (out / m.group(1)).resolve()
                    if not d.is_relative_to(out) or not (d / "meta.json").exists():
                        return self._json({"ok": False, "msg": "clip not found"}, 404)
                    plats = body.get("platforms")
                    if plats is not None and (not isinstance(plats, list) or any(x not in socials.PLATFORMS for x in plats)):
                        raise ValueError("platforms must be youtube / tiktok / instagram")
                    results = socials.upload_clip(app.cfg, d, plats, force=True)
                    ok = [k for k, v in results.items() if v.get("ok")]
                    bad = [f"{k}: {v.get('error')}" for k, v in results.items() if not v.get("ok")]
                    msg = ("uploaded to " + ", ".join(ok)) if ok else "upload failed"
                    if bad:
                        msg += (" · " if ok else "") + "; ".join(bad)
                    return self._json({"ok": bool(ok), "msg": msg, "uploads": results}, 200 if ok else 502)
                if p == "/api/streamers":
                    platform = body.get("platform", "twitch")
                    name = str(body.get("name", "")).strip()
                    if platform not in ("twitch", "youtube") or not re.fullmatch(r"@?[\w.-]{2,60}", name.rstrip("/").split("/")[-1]):
                        raise ValueError("enter a channel name like xqc or @IShowSpeed")
                    w = app.add_streamer(platform, name.rstrip("/").split("/")[-1])
                    return self._json({"ok": True, "msg": f"watching {w.s.display}"})
                if m := re.fullmatch(r"/api/streamers/([\w.@-]+)/toggle", p):
                    st = app.toggle_streamer(m.group(1))
                    if st is None:
                        return self._json({"ok": False, "msg": "not found"}, 404)
                    return self._json({"ok": True, "msg": "enabled" if st else "paused"})
                return self._json({"error": "not found"}, 404)
            except (ValueError, TypeError, json.JSONDecodeError) as e:
                return self._json({"ok": False, "msg": str(e)}, 400)

        def do_DELETE(self):
            if not self._guard():
                return
            if m := re.fullmatch(r"/api/clips/([\w.@-]+/[\w.-]+)", urlparse(self.path).path):
                d = (out / m.group(1)).resolve()
                if d.parent.parent != out or not (d / "meta.json").exists():
                    return self._json({"ok": False, "msg": "clip not found"}, 404)
                shutil.rmtree(d)
                return self._json({"ok": True, "msg": "clip deleted"})
            m = re.fullmatch(r"/api/streamers/([\w.@-]+)", urlparse(self.path).path)
            ok = bool(m) and app.remove_streamer(m.group(1))
            self._json({"ok": ok, "msg": "removed" if ok else "not found"}, 200 if ok else 404)

    return H


def serve(app, port: int):
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    srv.daemon_threads = True
    return srv
