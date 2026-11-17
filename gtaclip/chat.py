"""Live chat readers. Twitch: anonymous IRC (no key). YouTube: chat-downloader (no key)."""
from __future__ import annotations

import logging
import random
import re
import socket
import threading
import time
from collections import deque

log = logging.getLogger("chat")


def is_hype(text: str, words: list[str]) -> bool:
    """Whole word/phrase match ("w" hits "W W W", not "wow"). Same rule as gtacore's chat hub."""
    low = text.lower()
    return any(re.search(rf"(?<![^\W_]){re.escape(w.strip())}(?![^\W_])", low) for w in words if w.strip())


class ChatBase:
    def __init__(self, hype_words: list[str]):
        self.msgs: deque[tuple[float, str, str]] = deque(maxlen=4000)  # ~few minutes of chat; bounds RAM per streamer
        self.hype_words = [h.lower() for h in hype_words]
        self.baseline: float | None = None  # msgs/sec, slow moving average
        self.started = time.time()
        self._stop = threading.Event()
        self.connected = False

    def add(self, user: str, text: str) -> None:
        self.msgs.append((time.time(), user, text))

    def between(self, t0: float, t1: float) -> list[tuple[float, str, str]]:
        return [m for m in list(self.msgs) if t0 <= m[0] <= t1]

    def rate(self, window: float = 5.0) -> float:
        now = time.time()
        return sum(1 for m in reversed(self.msgs) if m[0] >= now - window) / window if self.msgs else 0.0

    def hype_ratio(self, window: float = 6.0) -> float:
        recent = self.between(time.time() - window, time.time())
        if not recent:
            return 0.0
        hits = sum(1 for _, _, t in recent if is_hype(t, self.hype_words))
        return hits / len(recent)

    def update_baseline(self) -> float:
        r = self.rate(10.0)
        if self.baseline is None:
            self.baseline = r
        elif r < (self.baseline * 2 + 1):  # don't let spikes pull the baseline up fast
            self.baseline = self.baseline * 0.97 + r * 0.03
        else:
            self.baseline = self.baseline * 0.995 + r * 0.005
        return self.baseline

    def warm(self) -> bool:
        return time.time() - self.started > 60

    def stop(self) -> None:
        self._stop.set()


class TwitchChat(ChatBase):
    def __init__(self, channel: str, hype_words: list[str]):
        super().__init__(hype_words)
        self.channel = channel.lower()
        threading.Thread(target=self._run, daemon=True, name=f"chat-{channel}").start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=30) as s:
                    nick = f"justinfan{random.randint(10000, 99999)}"
                    s.sendall(f"PASS SCHMOOPIIE\r\nNICK {nick}\r\nJOIN #{self.channel}\r\n".encode())
                    self.connected = True
                    buf = b""
                    while not self._stop.is_set():
                        try:
                            data = s.recv(65536)
                        except socket.timeout:
                            s.sendall(b"PING :tmi.twitch.tv\r\n")
                            continue
                        if not data:
                            break
                        buf += data
                        *lines, buf = buf.split(b"\r\n")
                        for raw in lines:
                            line = raw.decode("utf-8", "replace")
                            if line.startswith("PING"):
                                s.sendall(b"PONG :tmi.twitch.tv\r\n")
                            elif " PRIVMSG " in line:
                                user = line[1:line.find("!")]
                                text = line.split(" :", 1)[1] if " :" in line else ""
                                self.add(user, text)
            except OSError as e:
                log.warning("twitch chat %s: %s, reconnecting", self.channel, e)
            self.connected = False
            self._stop.wait(5)


class HubTwitchChat(ChatBase):
    """Twitch chat through the shared Rust hub (one IRC socket for all channels)."""

    def __init__(self, channel: str, hype_words: list[str], hub):
        super().__init__(hype_words)
        self.channel = channel.lower()
        self.hub = hub
        hub.join(self.channel, self._on_msg)

    def _on_msg(self, user: str, text: str) -> None:
        self.add(user, text)

    @property
    def connected(self) -> bool:  # type: ignore[override]
        return self.hub.connected

    @connected.setter
    def connected(self, v: bool) -> None:
        pass

    def stop(self) -> None:
        super().stop()
        self.hub.part(self.channel)


class YouTubeChat(ChatBase):
    """Reads YouTube live chat through the shared headless browser (pop-out chat page)."""

    def __init__(self, key: str, video_id: str, hype_words: list[str]):
        super().__init__(hype_words)
        from .browser import Browser

        self.key = key
        self.browser = Browser.get()
        if video_id:
            self.browser.watch_chat(key, video_id, self._on_msg)
        else:
            log.warning("no video id for %s; YouTube chat off", key)

    def _on_msg(self, user: str, text: str) -> None:
        self.connected = True
        self.add(user, text)

    def stop(self) -> None:
        super().stop()
        self.browser.unwatch_chat(self.key)


def make_chat(streamer, hype_words, video_id: str = "") -> ChatBase:
    if streamer.platform == "youtube":
        return YouTubeChat(streamer.key, video_id, hype_words)
    from .core import ChatHub

    hub = ChatHub.get(hype_words)
    if hub:
        return HubTwitchChat(streamer.name, hype_words, hub)
    return TwitchChat(streamer.name, hype_words)
