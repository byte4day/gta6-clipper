"""Animated word-by-word captions (.ass) + hook text + streamer tag."""
from __future__ import annotations

import re

# colors are ASS &HAABBGGRR
STYLES = {
    # Vice City sunset: white words, hot-pink active word, thick black outline
    "gta": dict(font="Bebas Neue", size=128, color="&H00FFFFFF", hi="&H00A53EFF", outline=8, shadow=3, upper=True, spacing=2),
    # Hormozi-style yellow highlight
    "bold": dict(font="Anton", size=104, color="&H00FFFFFF", hi="&H0000E5FF", outline=7, shadow=2, upper=True, spacing=1),
    # MrBeast cartoon look
    "mrbeast": dict(font="Luckiest Guy", size=100, color="&H00FFFFFF", hi="&H0014FF39", outline=9, shadow=4, upper=True, spacing=1),
    # clean sentence case
    "minimal": dict(font="Montserrat", size=70, color="&H00FFFFFF", hi="&H0066F2FF", outline=3, shadow=2, upper=False, spacing=0),
}

HOOK_BOX = {"gta": "&H00A53EFF", "bold": "&H0000E5FF", "mrbeast": "&H0014FF39", "minimal": "&H00FFFFFF"}


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _clean(s: str) -> str:
    return re.sub(r"[{}\\]", "", s).strip()


def group_words(words: list[dict], per: int = 3, max_gap: float = 0.7) -> list[list[dict]]:
    chunks, cur = [], []
    for w in words:
        if not _clean(w["word"]):
            continue
        if cur and (len(cur) >= per or w["start"] - cur[-1]["end"] > max_gap or re.search(r"[.!?]$", cur[-1]["word"])):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def build_ass(words: list[dict], duration: float, style: str = "gta", vertical: bool = True,
              hook: str = "", hook_seconds: float = 3.5, tag: str = "", per: int = 3) -> str:
    st = STYLES.get(style, STYLES["gta"])
    W, H = (1080, 1920) if vertical else (1920, 1080)
    cap_y = int(H * 0.70) if vertical else int(H * 0.82)
    scale = 1.0 if vertical else 0.75
    size = int(st["size"] * scale)
    hook_box = HOOK_BOX.get(style, "&H00A53EFF")
    hook_text_color = "&H00FFFFFF" if style != "minimal" else "&H00000000"
    hook_font = "Anton" if style != "mrbeast" else "Luckiest Guy"

    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{st['font']},{size},{st['color']},&H000000FF,&H00000000,&H96000000,-1,0,0,0,100,100,{st['spacing']},0,1,{st['outline']},{st['shadow']},5,60,60,0,1
Style: Hook,{hook_font},{int(84 * scale)},{hook_text_color},&H000000FF,{hook_box},&H00000000,-1,0,0,0,100,100,1,0,3,18,0,8,80,80,0,1
Style: Tag,Montserrat,{int(40 * scale)},&H40FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,2,1,2,40,40,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    if hook:
        hy = int(H * 0.12)
        ev.append(f"Dialogue: 2,{_ts(0)},{_ts(min(hook_seconds, duration))},Hook,,0,0,0,,"
                  f"{{\\pos({W // 2},{hy})\\fad(0,250)\\fscx70\\fscy70\\t(0,160,\\fscx106\\fscy106)\\t(160,260,\\fscx100\\fscy100)}}{_clean(hook)}")
    if tag:
        ev.append(f"Dialogue: 1,{_ts(0)},{_ts(duration)},Tag,,0,0,0,,{{\\pos({W // 2},{H - (150 if vertical else 40)})}}{_clean(tag)}")

    for chunk in group_words(words, per):
        texts = [_clean(w["word"]).upper() if st["upper"] else _clean(w["word"]) for w in chunk]
        chunk_end = min(chunk[-1]["end"] + 0.25, duration)
        for i, w in enumerate(chunk):
            start = w["start"]
            end = chunk[i + 1]["start"] if i + 1 < len(chunk) else chunk_end
            if end <= start:
                end = start + 0.08
            parts = []
            for j, t in enumerate(texts):
                if j == i:
                    parts.append(f"{{\\c{st['hi']}\\fscx114\\fscy114}}{t}{{\\r}}")
                else:
                    parts.append(t)
            pop = "\\fscx82\\fscy82\\t(0,90,\\fscx100\\fscy100)" if i == 0 else ""
            ev.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Cap,,0,0,0,,{{\\pos({W // 2},{cap_y}){pop}}}{' '.join(parts)}")
    return head + "\n".join(ev) + "\n"
