"""Shared headless browser (Playwright + Chrome Headless Shell).

Opens a channel page the way a viewer would, lets the real web player start, and captures
the HLS playlist the player requests. The recorder then downloads that stream directly.
One browser process serves every streamer; Playwright objects live on one dedicated thread.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future

log = logging.getLogger("browser")

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
_JS_CHAT = """() => { const out = [];
  document.querySelectorAll('yt-live-chat-text-message-renderer:not([data-gc])').forEach(e => {
    e.setAttribute('data-gc', '1');
    out.push([(e.querySelector('#author-name')?.textContent || '?').trim(), (e.querySelector('#message')?.innerText || '').trim()]);
  });
  return out; }"""
_JS_YT = "() => { try { return (window.ytInitialPlayerResponse||{}).streamingData?.hlsManifestUrl || null } catch (e) { return null } }"


class Browser:
    _inst: "Browser | None" = None
    _lock = threading.Lock()

    @classmethod
    def get(cls) -> "Browser":
        with cls._lock:
            if cls._inst is None:
                cls._inst = Browser()
            return cls._inst

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.chats: dict = {}  # key -> (page, callback): live chat pages read every ~1.5 s
        self.ok: bool | None = None  # None = starting, False = unavailable
        self.error = ""
        threading.Thread(target=self._run, daemon=True, name="browser").start()

    def resolve(self, url: str, timeout: float = 45) -> str | None:
        """Return the HLS master playlist URL the web player loads, or None."""
        if self.ok is False:
            return None
        fut: Future = Future()
        self.q.put(("resolve", url, fut))
        try:
            return fut.result(timeout=timeout)
        except Exception as e:
            log.warning("resolve %s: %s", url, str(e)[:120])
            return None

    def _run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.ok, self.error = False, "playwright not installed"
            log.warning("playwright missing; browser recording off (pip install playwright && playwright install chromium-headless-shell)")
            return
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=True, args=[
                "--mute-audio", "--autoplay-policy=no-user-gesture-required", "--disable-gpu",
                "--disable-dev-shm-usage", "--no-first-run", "--disable-background-networking"])
            ctx = browser.new_context(viewport={"width": 1280, "height": 720}, user_agent=UA, locale="en-US")
            # don't waste bandwidth on images/fonts: we only need the player's network requests
            ctx.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "font", "media") else r.continue_())
            self.ok = True
            log.info("headless browser ready (%s)", browser.version)
        except Exception as e:
            self.ok, self.error = False, str(e)[:200]
            log.error("browser failed to start: %s", self.error)
            return
        last_chat = 0.0
        while True:
            try:
                kind, a, b = self.q.get(timeout=0.5)
            except queue.Empty:
                kind = None
            try:
                if kind == "resolve" and not b.cancelled():
                    try:
                        b.set_result(self._capture(ctx, a))
                    except Exception as e:
                        b.set_exception(e)
                elif kind == "chat+":
                    self._open_chat(ctx, a, b)
                elif kind == "chat-" and a in self.chats:
                    page, _ = self.chats.pop(a)
                    page.close()
                if time.time() - last_chat > 1.5:
                    last_chat = time.time()
                    self._poll_chats()
            except Exception as e:
                log.warning("browser: %s", str(e)[:160])
            if not browser.is_connected():  # crashed: a new one starts on the next request
                self.ok = None
                Browser._inst = None
                return

    # ---- YouTube live chat (pop-out chat page, read from the DOM) ----------------
    def watch_chat(self, key: str, video_id: str, callback) -> None:
        url = f"https://www.youtube.com/live_chat?is_popout=1&v={video_id}"
        self.q.put(("chat+", key, (url, callback)))

    def unwatch_chat(self, key: str) -> None:
        self.q.put(("chat-", key, None))

    def _open_chat(self, ctx, key: str, arg) -> None:
        url, cb = arg
        if key in self.chats:
            self.chats.pop(key)[0].close()
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        self.chats[key] = (page, cb)
        log.info("reading YouTube chat for %s", key)

    def _poll_chats(self) -> None:
        for key, (page, cb) in list(self.chats.items()):
            try:
                for author, text in page.evaluate(_JS_CHAT):
                    cb(author, text)
            except Exception as e:
                log.warning("chat %s: %s", key, str(e)[:100])

    @staticmethod
    def _capture(ctx, url: str) -> str | None:
        page = ctx.new_page()
        found: list[str] = []
        page.on("request", lambda r: found.append(r.url) if ".m3u8" in r.url and "usher" in r.url else None)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            for i in range(50):  # up to ~25 s
                if found:
                    return found[0]
                hls = page.evaluate(_JS_YT)
                if hls:
                    return hls
                if i in (10, 20):  # mature-content / "start watching" gates
                    for label in ("Start Watching", "Start watching", "Accept"):
                        btn = page.get_by_role("button", name=label)
                        if btn.count():
                            btn.first.click(timeout=2000)
                page.wait_for_timeout(500)
            return None
        finally:
            page.close()
