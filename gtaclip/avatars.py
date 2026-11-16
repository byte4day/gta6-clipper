"""Streamer profile pictures from the public channel page (og:image). Cached in state/avatars/."""
from __future__ import annotations

import logging
import re
import time

import requests

from .config import ROOT

log = logging.getLogger("avatar")
DIR = ROOT / "state" / "avatars"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}


def channel_page(streamer) -> str:
    if streamer.platform == "youtube":
        return streamer.url.removesuffix("/live")
    return streamer.url


def avatar_path(key: str):
    return DIR / f"{key}.jpg"


def fetch_avatar(streamer, max_age_days: float = 7) -> bool:
    out = avatar_path(streamer.key)
    if out.exists() and time.time() - out.stat().st_mtime < max_age_days * 86400:
        return True
    try:
        html = requests.get(channel_page(streamer), headers=UA, timeout=20).text
        m = re.search(r'<meta (?:property|name)="og:image" content="([^"]+)"', html)
        if not m or "ttv-static" in m.group(1):  # twitch's generic image = channel not found
            return False
        img = requests.get(m.group(1).replace("&amp;", "&"), headers=UA, timeout=20)
        img.raise_for_status()
        DIR.mkdir(parents=True, exist_ok=True)
        out.write_bytes(img.content)
        return True
    except requests.RequestException as e:
        log.warning("avatar %s: %s", streamer.display, e)
        return False
