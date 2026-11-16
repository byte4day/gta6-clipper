"""Multi-provider LLM client (OpenAI-compatible) with fallback across free tiers."""
from __future__ import annotations

import json
import logging
import os
import re
import time

import requests

log = logging.getLogger("llm")

PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
}


class LLM:
    def __init__(self, cfg):
        llm = cfg["llm"]
        self.order = [p for p in llm.get("providers", PROVIDERS) if p in PROVIDERS]
        self.models = llm.get("models", {})
        self.cooldown: dict[str, float] = {}  # provider -> retry-after timestamp

    def available(self) -> list[str]:
        return [p for p in self.order if os.environ.get(PROVIDERS[p][1])]

    def chat(self, system: str, user: str, temperature: float = 0.7, max_tokens: int = 600) -> str | None:
        for p in self.available():
            if self.cooldown.get(p, 0) > time.time():
                continue
            base, env = PROVIDERS[p]
            try:
                r = requests.post(
                    f"{base}/chat/completions",
                    headers={"Authorization": f"Bearer {os.environ[env]}", "Content-Type": "application/json"},
                    json={
                        "model": self.models.get(p),
                        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    },
                    timeout=40,
                )
                if r.status_code == 429:
                    wait = float(r.headers.get("retry-after", 30) or 30)
                    self.cooldown[p] = time.time() + min(wait, 300)
                    log.warning("%s rate-limited, next provider", p)
                    continue
                if r.status_code >= 400:
                    detail = (r.text or "")[:160].replace("\n", " ")
                    log.warning("%s HTTP %s: %s", p, r.status_code, detail)
                    self.cooldown[p] = time.time() + (60 if r.status_code == 404 else 20)
                    continue
                msg = (r.json().get("choices") or [{}])[0].get("message") or {}
                text = (msg.get("content") or "").strip()
                if text:
                    return text
                # Free reasoning models often spend the whole budget on reasoning and leave content empty
                log.warning("%s empty content (model=%s)", p, self.models.get(p))
            except Exception as e:  # network/provider errors -> try next provider
                log.warning("%s failed: %s", p, str(e)[:160])
                self.cooldown[p] = time.time() + 20
        return None

    def json(self, system: str, user: str, **kw) -> dict | None:
        text = self.chat(system + "\nReply with ONE JSON object only, no markdown.", user, **kw)
        if not text:
            return None
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


META_SYSTEM = (
    "You write viral short-form clip packaging for GTA 6 streamer clips (TikTok, YouTube Shorts, Reels). "
    "Be punchy, Gen-Z, no cringe, no fake claims beyond the transcript and chat."
)


def clip_metadata(llm: LLM, streamer: str, trigger: str, transcript: str, chat: list[str], title: str, category: str) -> dict:
    user = (
        f"Streamer: {streamer}\nStream title: {title}\nGame: {category}\nWhy clipped: {trigger}\n"
        f"Transcript: {transcript[:1800] or '(no speech)'}\n"
        f"Chat during clip: {' | '.join(chat[-40:])[:1200]}\n\n"
        'Return {"title": str (<70 chars, viral), "hook": str (2-6 words ALL CAPS shown on screen at start), '
        '"caption": str (1-2 sentences for the post), "hashtags": [8-12 strings with #, mix of #gta6 #gtavi, streamer, niche and broad], '
        '"score": int 0-10 (how viral)}'
    )
    data = llm.json(META_SYSTEM, user, temperature=0.8) if llm.available() else None
    if data and data.get("title"):
        tags = [t if t.startswith("#") else "#" + t for t in data.get("hashtags", []) if t]
        data["hashtags"] = [t.replace(" ", "") for t in tags][:14]
        return data
    return fallback_metadata(streamer, trigger, transcript)


TRIGGER_TITLES = {
    "stream_start": ("{s} JUST WENT LIVE", "First minute of {s}'s stream"),
    "gta6_open": ("FIRST TIME IN GTA 6", "{s} opens GTA 6 for the first time"),
    "chat_spike": ("CHAT WENT CRAZY", "{s} made chat lose it"),
    "audio_spike": ("HE LOST IT", "{s} completely loses it"),
    "ai_moment": ("WAIT FOR IT", "{s} had the craziest moment"),
    "manual": ("WATCH THIS", "{s} live moment"),
}


def fallback_metadata(streamer: str, trigger: str, transcript: str) -> dict:
    hook, title = TRIGGER_TITLES.get(trigger.split(":")[0], TRIGGER_TITLES["manual"])
    s = streamer
    tag = "#" + re.sub(r"\W", "", streamer.lower())
    return {
        "title": title.format(s=s) + " 😭 #GTA6",
        "hook": hook.format(s=s.upper()),
        "caption": (transcript[:140] + "…") if transcript else title.format(s=s),
        "hashtags": ["#gta6", "#gtavi", tag, "#grandtheftauto", "#gaming", "#twitchclips",
                     "#streamer", "#fyp", "#viral", "#clips"],
        "score": 5,
        "fallback": True,
    }


SCAN_SYSTEM = (
    "You watch live GTA 6 streams through transcript + chat and decide if the last 30 seconds is a clip-worthy moment "
    "(funny, shocking, rage, hype, famous catchphrase, big reveal, first reactions). Most windows are NOT clip-worthy."
)


def scan_moment(llm: LLM, streamer: str, transcript: str, chat: list[str], category: str) -> dict | None:
    if not llm.available() or (not transcript.strip() and len(chat) < 5):
        return None
    user = (
        f"Streamer: {streamer}  Game: {category}\nLast 30s transcript: {transcript[:1500]}\n"
        f"Chat last 30s ({len(chat)} msgs): {' | '.join(chat[-60:])[:1500]}\n"
        'Return {"score": int 0-10, "reason": str (max 12 words)}'
    )
    return llm.json(SCAN_SYSTEM, user, temperature=0.2, max_tokens=120)
