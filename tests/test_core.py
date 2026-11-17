"""Fast offline tests: HLS parsing, quality pick, captions, GTA detection, settings validation."""
import re

import pytest

from gtaclip.captions import build_ass, group_words
from gtaclip.config import load_config
from gtaclip.recorder import parse_master, pick_variant
from gtaclip.web import coerce

MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="1080p60",NAME="1080p60"
#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080,FRAME-RATE=60.000,VIDEO="1080p60"
https://x/1080.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3400000,RESOLUTION=1280x720,FRAME-RATE=60.000,VIDEO="720p60"
https://x/720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1400000,RESOLUTION=852x480,FRAME-RATE=30.000,VIDEO="480p30"
https://x/480.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=160000,CODECS="mp4a.40.2",VIDEO="audio_only"
https://x/audio.m3u8
"""


def test_parse_master_and_pick():
    vs = parse_master(MASTER, "https://x/master.m3u8")
    assert [v["name"] for v in vs] == ["1080p60", "720p60", "480p30", "audio_only"]
    assert pick_variant(vs, "720p60,720p,best")["name"] == "720p60"
    assert pick_variant(vs, "480p,best")["name"] == "480p30"
    assert pick_variant(vs, "best")["name"] == "1080p60"
    assert pick_variant(vs, "360p,worst")["name"] == "480p30"


def test_caption_grouping_and_ass():
    words = [{"word": w, "start": i * 0.4, "end": i * 0.4 + 0.3} for i, w in enumerate("no way he did that bro {x}".split())]
    chunks = group_words(words, per=3)
    assert [len(c) for c in chunks] == [3, 3, 1]
    ass = build_ass(words, 5.0, "gta", True, hook="FIRST TIME IN GTA 6", tag="twitch.tv/xqc")
    assert "PlayResX: 1080" in ass and "FIRST TIME IN GTA 6" in ass
    assert ass.count("Style: Cap,Bebas Neue") == 1
    assert "{x}" not in ass  # braces stripped so ASS override tags can't be injected
    assert len(re.findall(r"^Dialogue: 0,", ass, re.M)) == 7  # one event per word


def test_gta_pattern():
    pat = re.compile(load_config()["detect"]["gta_pattern"], re.I)
    for yes in ("Grand Theft Auto VI", "FIRST TIME PLAYING GTA 6!!", "gta vi day 1", "GTA6 RP"):
        assert pat.search(yes), yes
    for no in ("Grand Theft Auto V", "GTA V RP NoPixel", "World of Warcraft"):
        assert not pat.search(no), no


def test_settings_validation():
    assert coerce(int, None, "5") == 5
    assert coerce(bool, None, "true") is True
    assert coerce(list, ["vertical", "landscape"], ["vertical", "evil"]) == ["vertical"]
    with pytest.raises(ValueError):
        coerce(str, ["gta", "bold"], "nope")
    with pytest.raises(ValueError):
        coerce(list, ["vertical"], [])
    assert coerce(list, ["youtube", "tiktok", "instagram"], ["youtube", "myspace"]) == ["youtube"]
    assert coerce(str, ["public", "unlisted", "private"], "unlisted") == "unlisted"


def test_socials_post_helpers():
    from gtaclip.socials import TIKTOK_PRIVACY, _connected, _html_esc, description, post_text, redirect_uri

    title, caption, tags = post_text({
        "title": "INSANE GTA 6 CRASH",
        "caption": "bro what",
        "hashtags": ["#gta6", "clips", "##viral"],
        "streamer": "xqc",
    })
    assert title.startswith("INSANE")
    assert tags == ["gta6", "clips", "viral"]
    desc = description({"title": title, "caption": caption, "hashtags": ["#" + t for t in tags], "streamer": "xqc"})
    assert "bro what" in desc and "#gta6" in desc and "xqc" in desc
    assert redirect_uri(8787, "youtube").endswith("/api/socials/callback/youtube")
    assert _connected({"refresh_token": "x"}) and not _connected({})
    assert TIKTOK_PRIVACY["private"] == "SELF_ONLY"
    assert "&lt;" in _html_esc("<script>")


def test_hype_whole_words():
    from gtaclip.chat import is_hype

    words = ["clip it", "w ", "kekw"]
    assert is_hype("CLIP IT NOW", words)
    assert is_hype("W W W", words)
    assert is_hype("kekw", words)
    assert not is_hype("wow", words)
    assert not is_hype("kekwait", words)


def test_rust_core_matches_python():
    from gtaclip import core

    if not core.available():
        pytest.skip("gtacore not built")
    assert core.ts_duration(__import__("pathlib").Path("/nonexistent.ts")) is None
    assert core.live_status([]) == {}


def test_all_configured_streamers_are_valid():
    cfg = load_config()
    keys = [s.key for s in cfg.streamers]
    assert len(keys) == len(set(keys)) >= 30
    assert all(s.platform in ("twitch", "youtube") for s in cfg.streamers)
