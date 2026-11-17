"""Connect YouTube / TikTok / Instagram and upload finished clips."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
from base64 import urlsafe_b64encode
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlencode

import requests

from .config import ROOT, set_env_key

log = logging.getLogger("socials")

TOKENS_FILE = ROOT / "state" / "socials.json"
PLATFORMS = ("youtube", "tiktok", "instagram")
_tls = threading.local()


def _http() -> requests.Session:
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        _tls.session = s
    return s


ENV_KEYS = {
    "youtube": ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET"),
    "tiktok": ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"),
    "instagram": ("INSTAGRAM_APP_ID", "INSTAGRAM_APP_SECRET"),
}

DOCS = {
    "youtube": "https://console.cloud.google.com/apis/credentials",
    "tiktok": "https://developers.tiktok.com/apps",
    "instagram": "https://developers.facebook.com/apps",
}

SCOPES = {
    "youtube": "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly",
    "tiktok": "user.info.basic,video.upload,video.publish",
    # Instagram Graph publishing needs a Page-linked Business/Creator account
    "instagram": "instagram_basic,instagram_content_publish,pages_show_list,pages_read_engagement,business_management",
}

TIKTOK_PRIVACY = {
    "public": "PUBLIC_TO_EVERYONE",
    "friends": "MUTUAL_FOLLOW_FRIENDS",
    "private": "SELF_ONLY",
    "SELF_ONLY": "SELF_ONLY",
    "PUBLIC_TO_EVERYONE": "PUBLIC_TO_EVERYONE",
    "MUTUAL_FOLLOW_FRIENDS": "MUTUAL_FOLLOW_FRIENDS",
    "FOLLOWER_OF_CREATOR": "FOLLOWER_OF_CREATOR",
}

_pending: dict[str, dict] = {}
_lock = threading.Lock()
_clip_locks: dict[str, threading.Lock] = {}
_clip_locks_guard = threading.Lock()


def _clip_lock(clip_dir: Path) -> threading.Lock:
    key = str(clip_dir.resolve())
    with _clip_locks_guard:
        return _clip_locks.setdefault(key, threading.Lock())


def _load() -> dict:
    if not TOKENS_FILE.exists():
        return {}
    try:
        return json.loads(TOKENS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    TOKENS_FILE.parent.mkdir(exist_ok=True)
    tmp = TOKENS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.chmod(0o600)
    tmp.replace(TOKENS_FILE)


def _creds(platform: str) -> tuple[str, str]:
    a, b = ENV_KEYS[platform]
    return os.environ.get(a, "").strip(), os.environ.get(b, "").strip()


def redirect_uri(port: int, platform: str) -> str:
    return f"http://127.0.0.1:{port}/api/socials/callback/{platform}"


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def status_payload(cfg, port: int) -> dict:
    data = _load()
    s = cfg["socials"] if hasattr(cfg, "__getitem__") else {}
    platforms = []
    for p in PLATFORMS:
        cid, secret = _creds(p)
        acct = data.get(p) or {}
        platforms.append({
            "id": p,
            "label": {"youtube": "YouTube Shorts", "tiktok": "TikTok", "instagram": "Instagram Reels"}[p],
            "has_app": bool(cid and secret),
            "connected": _connected(acct),
            "account": acct.get("account") or acct.get("channel") or "",
            "client_id_masked": (cid[:6] + "…" + cid[-4:]) if len(cid) > 12 else ("set" if cid else ""),
            "docs": DOCS[p],
            "env": list(ENV_KEYS[p]),
        })
    return {
        "platforms": platforms,
        "auto_upload": bool(s.get("auto_upload", True)),
        "targets": list(s.get("platforms", list(PLATFORMS))),
        "privacy": s.get("privacy", "public"),
        "redirect_base": f"http://127.0.0.1:{port}/api/socials/callback/",
    }


def save_app_creds(platform: str, client_id: str | None = None, client_secret: str | None = None) -> None:
    if platform not in PLATFORMS:
        raise ValueError("unknown platform")
    a, b = ENV_KEYS[platform]
    cur_id, cur_secret = _creds(platform)
    if client_id is not None:
        set_env_key(a, client_id.strip())
        cur_id = client_id.strip()
    if client_secret is not None:
        set_env_key(b, client_secret.strip())
        cur_secret = client_secret.strip()
    if not cur_id or not cur_secret:
        raise ValueError(f"both {a} and {b} are required")


def disconnect(platform: str) -> None:
    with _lock:
        data = _load()
        data.pop(platform, None)
        _save(data)


def start_oauth(platform: str, port: int) -> str:
    if platform not in PLATFORMS:
        raise ValueError("unknown platform")
    cid, secret = _creds(platform)
    if not cid or not secret:
        raise ValueError(f"add {ENV_KEYS[platform][0]} + {ENV_KEYS[platform][1]} first")
    state = secrets.token_urlsafe(24)
    verifier, challenge = _pkce()
    now = time.time()
    with _lock:
        for k, v in list(_pending.items()):
            if now - v["at"] > 900:
                _pending.pop(k, None)
        _pending[state] = {"platform": platform, "verifier": verifier, "at": now}
    redir = redirect_uri(port, platform)
    if platform == "youtube":
        q = urlencode({
            "client_id": cid,
            "redirect_uri": redir,
            "response_type": "code",
            "scope": SCOPES[platform],
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        })
        return f"https://accounts.google.com/o/oauth2/v2/auth?{q}"
    if platform == "tiktok":
        q = urlencode({
            "client_key": cid,
            "redirect_uri": redir,
            "response_type": "code",
            "scope": SCOPES[platform],
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        return f"https://www.tiktok.com/v2/auth/authorize/?{q}"
    q = urlencode({
        "client_id": cid,
        "redirect_uri": redir,
        "response_type": "code",
        "scope": SCOPES[platform],
        "state": state,
    })
    return f"https://www.facebook.com/v21.0/dialog/oauth?{q}"


def finish_oauth(platform: str, code: str, state: str, port: int) -> str:
    with _lock:
        pending = _pending.pop(state, None)
    if not pending or pending["platform"] != platform:
        raise ValueError("invalid or expired OAuth state — try Connect again")
    cid, secret = _creds(platform)
    redir = redirect_uri(port, platform)
    if platform == "youtube":
        r = _http().post("https://oauth2.googleapis.com/token", data={
            "code": code, "client_id": cid, "client_secret": secret,
            "redirect_uri": redir, "grant_type": "authorization_code",
        }, timeout=30)
        r.raise_for_status()
        tok = r.json()
        if not tok.get("access_token"):
            raise ValueError(tok.get("error_description") or tok.get("error") or "YouTube token exchange failed")
        account = _yt_channel(tok["access_token"])
        _store(platform, tok, account=account)
        return account or "YouTube"
    if platform == "tiktok":
        r = _http().post("https://open.tiktokapis.com/v2/oauth/token/", data={
            "client_key": cid, "client_secret": secret, "code": code,
            "grant_type": "authorization_code", "redirect_uri": redir,
            "code_verifier": pending["verifier"],
        }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
        r.raise_for_status()
        tok = r.json()
        if tok.get("error") and tok.get("error") != "ok":
            raise ValueError(tok.get("error_description") or tok["error"])
        if not tok.get("access_token"):
            raise ValueError("TikTok token exchange failed")
        account = _tt_user(tok["access_token"])
        _store(platform, tok, account=account)
        return account or "TikTok"
    # Instagram via Facebook Login
    r = _http().get("https://graph.facebook.com/v21.0/oauth/access_token", params={
        "client_id": cid, "client_secret": secret, "redirect_uri": redir, "code": code,
    }, timeout=30)
    r.raise_for_status()
    short = r.json()
    if short.get("error"):
        err = short["error"]
        raise ValueError(err.get("message") if isinstance(err, dict) else str(err))
    long_lived = _http().get("https://graph.facebook.com/v21.0/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": cid, "client_secret": secret,
        "fb_exchange_token": short["access_token"],
    }, timeout=30)
    long_lived.raise_for_status()
    tok = long_lived.json()
    if not tok.get("access_token"):
        raise ValueError("Instagram long-lived token exchange failed")
    ig_user, page_token, account = _ig_account(tok["access_token"])
    if not ig_user:
        raise ValueError("no Instagram Business/Creator account linked to a Facebook Page")
    with _lock:
        data = _load()
        data[platform] = {
            "access_token": page_token or tok["access_token"],
            "user_token": tok["access_token"],
            "expires_at": int(time.time()) + int(tok.get("expires_in", 5_184_000)) - 60,
            "ig_user_id": ig_user,
            "account": account,
        }
        _save(data)
    return account or "Instagram"


def _store(platform: str, tok: dict, account: str = "", **extra) -> dict:
    with _lock:
        data = _load()
        prev = data.get(platform) or {}
        entry = {
            "access_token": tok.get("access_token") or prev.get("access_token"),
            "refresh_token": tok.get("refresh_token") or prev.get("refresh_token"),
            "expires_at": int(time.time()) + int(tok.get("expires_in", 3600)) - 60,
            "account": account or prev.get("account") or "",
        }
        if tok.get("open_id"):
            entry["open_id"] = tok["open_id"]
        for k in ("ig_user_id", "user_token"):
            if prev.get(k):
                entry[k] = prev[k]
        entry.update(extra)
        data[platform] = entry
        _save(data)
        return dict(entry)


def _yt_channel(token: str) -> str:
    r = _http().get("https://www.googleapis.com/youtube/v3/channels",
                  params={"part": "snippet", "mine": "true"},
                  headers={"Authorization": f"Bearer {token}"}, timeout=20)
    if not r.ok:
        return ""
    items = r.json().get("items") or []
    return (items[0]["snippet"]["title"] if items else "") or ""


def _tt_user(token: str) -> str:
    r = _http().get("https://open.tiktokapis.com/v2/user/info/",
                  params={"fields": "display_name,username"},
                  headers={"Authorization": f"Bearer {token}"}, timeout=20)
    if not r.ok:
        return ""
    u = (r.json().get("data") or {}).get("user") or {}
    return u.get("display_name") or u.get("username") or ""


def _ig_account(user_token: str) -> tuple[str, str, str]:
    pages = _http().get("https://graph.facebook.com/v21.0/me/accounts",
                      params={"fields": "id,name,access_token,instagram_business_account{id,username}",
                              "access_token": user_token}, timeout=30)
    pages.raise_for_status()
    for page in pages.json().get("data") or []:
        ig = page.get("instagram_business_account") or {}
        if ig.get("id"):
            return ig["id"], page.get("access_token", ""), ig.get("username") or page.get("name", "")
    return "", "", ""


def _connected(acct: dict | None) -> bool:
    acct = acct or {}
    return bool(acct.get("access_token") or acct.get("refresh_token"))


def _token(platform: str) -> dict:
    with _lock:
        acct = dict((_load().get(platform) or {}))
    if not _connected(acct):
        raise ValueError(f"{platform} is not connected")
    fresh = acct.get("expires_at", 0) > time.time() + 30 and acct.get("access_token")
    if platform == "instagram":
        if not acct.get("access_token") or not acct.get("ig_user_id"):
            raise ValueError("Instagram needs reconnect")
        if not fresh:
            raise ValueError("Instagram token expired — reconnect in Settings → Socials")
        return acct
    if fresh:
        return acct
    return _refresh(platform, acct)


def _refresh(platform: str, acct: dict) -> dict:
    cid, secret = _creds(platform)
    if platform == "youtube":
        if not acct.get("refresh_token"):
            raise ValueError("YouTube needs reconnect (no refresh token)")
        r = _http().post("https://oauth2.googleapis.com/token", data={
            "client_id": cid, "client_secret": secret,
            "refresh_token": acct["refresh_token"], "grant_type": "refresh_token",
        }, timeout=30)
        r.raise_for_status()
        tok = r.json()
        if not tok.get("access_token"):
            raise ValueError(tok.get("error_description") or "YouTube refresh failed")
        tok["refresh_token"] = acct["refresh_token"]
        return _store(platform, tok, account=acct.get("account", ""))
    if platform == "tiktok":
        if not acct.get("refresh_token"):
            raise ValueError("TikTok needs reconnect (no refresh token)")
        r = _http().post("https://open.tiktokapis.com/v2/oauth/token/", data={
            "client_key": cid, "client_secret": secret,
            "grant_type": "refresh_token", "refresh_token": acct["refresh_token"],
        }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
        r.raise_for_status()
        tok = r.json()
        if (tok.get("error") and tok.get("error") != "ok") or not tok.get("access_token"):
            raise ValueError(tok.get("error_description") or tok.get("error") or "TikTok refresh failed")
        return _store(platform, tok, account=acct.get("account", ""))
    return acct


def post_text(meta: dict) -> tuple[str, str, list[str]]:
    title = (meta.get("title") or "GTA 6 clip").strip()[:100]
    caption = (meta.get("caption") or "").strip()
    tags = [t.lstrip("#") for t in (meta.get("hashtags") or []) if t][:20]
    return title, caption, tags


def description(meta: dict) -> str:
    title, caption, tags = post_text(meta)
    bits = [title, "", caption, "", " ".join("#" + t for t in tags)]
    streamer = meta.get("streamer")
    if streamer:
        bits += ["", f"Clip from {streamer}"]
    return "\n".join(bits).strip()


def video_path(clip_dir: Path, meta: dict | None = None) -> Path:
    meta = meta or json.loads((clip_dir / "meta.json").read_text())
    outs = meta.get("outputs") or {}
    name = outs.get("vertical") or next(iter(outs.values()), None)
    if not name:
        raise ValueError("clip has no rendered video yet")
    path = clip_dir / name
    if not path.is_file():
        raise ValueError(f"missing {name}")
    return path


def _write_uploads(clip_dir: Path, results: dict) -> None:
    meta_path = clip_dir / "meta.json"
    with _lock:
        meta = json.loads(meta_path.read_text())
        meta["uploads"] = results
        tmp = meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2))
        tmp.replace(meta_path)


def _do_upload(platform: str, cfg, path: Path, meta: dict) -> dict:
    if platform == "youtube":
        return upload_youtube(cfg, path, meta)
    if platform == "tiktok":
        return upload_tiktok(cfg, path, meta)
    if platform == "instagram":
        return upload_instagram(path, meta)
    return {"ok": False, "error": "unknown platform", "at": int(time.time())}


def upload_clip(cfg, clip_dir: Path, platforms: list[str] | None = None, *, force: bool = False) -> dict:
    """Upload one finished clip to the given platforms (or configured targets).

    Platforms run in parallel. Already-successful uploads are skipped unless force=True.
    """
    clip_dir = Path(clip_dir)
    with _clip_lock(clip_dir):
        meta = json.loads((clip_dir / "meta.json").read_text())
        if meta.get("status") != "done":
            raise ValueError("clip is still being made")
        s = cfg["socials"]
        if platforms is None:
            targets = [p for p in s.get("platforms", list(PLATFORMS)) if p in PLATFORMS]
        else:
            targets = [p for p in platforms if p in PLATFORMS]
        if not targets:
            raise ValueError("no social platforms selected")
        path = video_path(clip_dir, meta)
        results = dict(meta.get("uploads") or {})
        todo = [p for p in targets if force or not (results.get(p) or {}).get("ok")]
        if not todo:
            return results

        def one(p: str) -> tuple[str, dict]:
            try:
                return p, _do_upload(p, cfg, path, meta)
            except Exception as e:
                log.exception("upload %s failed", p)
                return p, {"ok": False, "error": str(e)[:240], "at": int(time.time())}

        # Parallel put: YouTube + TikTok + IG at once (streamed, no full-file RAM buffer)
        with ThreadPoolExecutor(max_workers=min(3, len(todo)), thread_name_prefix="social-up") as pool:
            futs = [pool.submit(one, p) for p in todo]
            for fut in as_completed(futs):
                p, res = fut.result()
                results[p] = res
                _write_uploads(clip_dir, results)
        return results


def upload_youtube(cfg, path: Path, meta: dict) -> dict:
    acct = _token("youtube")
    title, _, tags = post_text(meta)
    privacy = cfg["socials"].get("privacy", "public")
    if privacy not in ("public", "unlisted", "private"):
        privacy = "public"
    body = {
        "snippet": {
            "title": title[:100],
            "description": description(meta)[:4900],
            "tags": tags[:15],
            "categoryId": "20",  # Gaming
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    size = path.stat().st_size
    init = _http().post(
        "https://www.googleapis.com/upload/youtube/v3/videos",
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={
            "Authorization": f"Bearer {acct['access_token']}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": "video/mp4",
        },
        json=body,
        timeout=60,
    )
    if init.status_code not in (200, 201):
        raise ValueError(f"YouTube init failed: {init.text[:200]}")
    loc = init.headers.get("Location")
    if not loc:
        raise ValueError("YouTube did not return an upload URL")
    with open(path, "rb") as f:
        put = _http().put(loc, data=f, headers={
            "Authorization": f"Bearer {acct['access_token']}",
            "Content-Type": "video/mp4",
            "Content-Length": str(size),
        }, timeout=600)
    if put.status_code not in (200, 201):
        raise ValueError(f"YouTube upload failed: {put.text[:200]}")
    vid = (put.json() or {}).get("id", "")
    return {"ok": True, "id": vid, "url": f"https://youtube.com/shorts/{vid}" if vid else "", "at": int(time.time())}


def upload_tiktok(cfg, path: Path, meta: dict) -> dict:
    acct = _token("tiktok")
    title, caption, tags = post_text(meta)
    text = (caption or title)[:2200]
    if tags:
        text = (text + "\n" + " ".join("#" + t for t in tags)).strip()[:2200]
    size = path.stat().st_size
    # Unaffected / unaudited TikTok apps can only post SELF_ONLY
    raw_priv = str(cfg["socials"].get("tiktok_privacy") or cfg["socials"].get("privacy") or "private")
    privacy = TIKTOK_PRIVACY.get(raw_priv, "SELF_ONLY")
    init = _http().post(
        "https://open.tiktokapis.com/v2/post/publish/video/init/",
        headers={
            "Authorization": f"Bearer {acct['access_token']}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json={
            "post_info": {
                "title": text[:150],
                "privacy_level": privacy,
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": size,
                "total_chunk_count": 1,
            },
        },
        timeout=60,
    )
    try:
        payload = init.json()
    except ValueError:
        payload = {}
    err = payload.get("error") or {}
    if init.status_code >= 400 or (isinstance(err, dict) and err.get("code", "ok") not in ("ok", 0, "0", "")):
        msg = err.get("message") if isinstance(err, dict) else None
        raise ValueError(f"TikTok init failed: {msg or init.text[:200]}")
    data = payload.get("data") or {}
    upload_url = data.get("upload_url")
    publish_id = data.get("publish_id", "")
    if not upload_url:
        raise ValueError("TikTok did not return an upload URL")
    with open(path, "rb") as f:
        put = _http().put(upload_url, data=f, headers={
            "Content-Type": "video/mp4",
            "Content-Length": str(size),
            "Content-Range": f"bytes 0-{size - 1}/{size}",
        }, timeout=600)
    if put.status_code not in (200, 201):
        raise ValueError(f"TikTok upload failed: {put.text[:200]}")
    return {"ok": True, "id": publish_id, "url": "", "at": int(time.time()), "note": "processing on TikTok",
            "privacy": privacy}


def upload_instagram(path: Path, meta: dict) -> dict:
    acct = _token("instagram")
    ig = acct.get("ig_user_id")
    token = acct.get("access_token")
    if not ig or not token:
        raise ValueError("Instagram is not connected")
    title, _, _ = post_text(meta)
    cap = description(meta)[:2200]
    size = path.stat().st_size
    # Resumable Reels upload (no public URL needed)
    start = _http().post(
        f"https://graph.facebook.com/v21.0/{ig}/media",
        data={
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": cap,
            "access_token": token,
        },
        timeout=60,
    )
    try:
        session = start.json()
    except ValueError:
        session = {}
    if start.status_code >= 400 or session.get("error"):
        err = session.get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else start.text[:240]
        raise ValueError(f"Instagram init failed: {msg}")
    container_id = session.get("id")
    upload_uri = (session.get("uri") or session.get("upload_uri")
                  or ((session.get("video_upload_urls") or [{}])[0] or {}).get("url"))
    if not container_id:
        raise ValueError(f"Instagram init response missing id: {session}")
    if not upload_uri:
        raise ValueError("Instagram did not return an upload URI — check app permissions")
    with open(path, "rb") as f:
        put = _http().post(upload_uri, data=f, headers={
            "Authorization": f"OAuth {token}",
            "offset": "0",
            "file_size": str(size),
            "Content-Type": "application/octet-stream",
        }, timeout=600)
    if put.status_code >= 400:
        raise ValueError(f"Instagram upload failed: {put.text[:200]}")
    # Wait until Instagram finishes processing the container, then publish
    deadline = time.time() + 180
    delay = 1.0
    while time.time() < deadline:
        st = _http().get(f"https://graph.facebook.com/v21.0/{container_id}",
                       params={"fields": "status_code,status", "access_token": token}, timeout=30)
        code = (st.json() or {}).get("status_code", "")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise ValueError(f"Instagram processing error: {st.text[:200]}")
        time.sleep(delay)
        delay = min(delay * 1.6, 5.0)
    else:
        raise ValueError("Instagram timed out processing the reel")
    pub = _http().post(
        f"https://graph.facebook.com/v21.0/{ig}/media_publish",
        data={"creation_id": container_id, "access_token": token},
        timeout=60,
    )
    try:
        pub_body = pub.json()
    except ValueError:
        pub_body = {}
    if pub.status_code >= 400 or pub_body.get("error"):
        err = pub_body.get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else pub.text[:200]
        raise ValueError(f"Instagram publish failed: {msg}")
    mid = pub_body.get("id", "")
    return {"ok": True, "id": mid, "url": f"https://www.instagram.com/reel/{mid}/" if mid else "",
            "at": int(time.time()), "title": title}


def auto_upload_if_enabled(app, clip_dir: Path) -> None:
    """Fire-and-forget after a clip finishes. Safe to call from a worker thread."""
    try:
        s = app.cfg["socials"]
        if not s.get("auto_upload", True):
            return
        data = _load()
        connected = [p for p in s.get("platforms", list(PLATFORMS)) if p in PLATFORMS and _connected(data.get(p))]
        if not connected:
            return
        log.info("auto-uploading %s → %s", clip_dir.name, ", ".join(connected))
        results = upload_clip(app.cfg, clip_dir, connected, force=False)
        ok = [p for p, r in results.items() if r.get("ok")]
        bad = [f"{p}: {r.get('error')}" for p, r in results.items() if not r.get("ok")]
        if ok:
            log.info("uploaded to %s", ", ".join(ok))
        for b in bad:
            log.warning("upload failed · %s", b)
    except Exception:
        log.exception("auto-upload failed")


def _html_esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def connected_html(account: str, error: str | None = None) -> bytes:
    if error:
        body = (f"<h1>Connect failed</h1><p>{_html_esc(error)}</p>"
                "<p>Close this tab and try again from Settings → Socials.</p>")
        color = "#ff2d55"
    else:
        body = f"<h1>Connected</h1><p>{_html_esc(account)} is linked. You can close this tab.</p>"
        color = "#3ddc97"
    html = f"""<!doctype html><html><head><meta charset=utf-8><title>Socials</title>
<style>body{{font-family:system-ui;background:#0b0911;color:#f4eefb;display:grid;place-items:center;min-height:100vh;margin:0}}
main{{background:#15111c;border:1px solid #2a2236;border-radius:16px;padding:28px 32px;max-width:420px}}
h1{{color:{color};font-size:22px;margin:0 0 10px}}p{{color:#9d90b0;line-height:1.5}}</style></head>
<body><main>{body}<script>setTimeout(()=>window.close(),1500)</script></main></body></html>"""
    return html.encode()
