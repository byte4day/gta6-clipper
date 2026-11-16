#!/usr/bin/env python3
"""Builds the animated README art in assets/readme/ (SVG with CSS animation).

Run:  python3 assets/readme/make.py
Needs: uvx (fonttools + brotli are pulled on demand) and Pillow for the screenshots.
"""
from __future__ import annotations

import base64
import html
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FONTS = HERE.parent / "fonts"
CAPTURES = Path.home() / "Videos/gta6-clipper/captures"

PINK, ORANGE, GOLD, NIGHT = "#ff3ea5", "#ff8a3d", "#ffd23d", "#0b0911"
MONO = "ui-monospace, 'SFMono-Regular', 'JetBrains Mono', Menlo, Consolas, 'DejaVu Sans Mono', monospace"
REDUCED = "@media (prefers-reduced-motion: reduce){*{animation:none!important}}"


def font_face(family: str, file: str, text: str) -> str:
    """Subset a font to the characters used and inline it as WOFF2."""
    chars = "".join(sorted(set(text + " ")))
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "f.woff2"
        subprocess.run(
            ["uvx", "--from", "fonttools", "--with", "brotli", "pyftsubset", str(FONTS / file),
             f"--text={chars}", "--flavor=woff2", f"--output-file={out}", "--layout-features=*"],
            check=True, capture_output=True,
        )
        data = base64.b64encode(out.read_bytes()).decode()
    return f"@font-face{{font-family:'{family}';src:url(data:font/woff2;base64,{data}) format('woff2');}}"


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def write(name: str, svg: str) -> None:
    (HERE / name).write_text(svg)
    print(f"{name:22} {len(svg.encode()) / 1024:6.1f} KB")


# ─── palm tree silhouette (drawn once, reused) ───────────────────────────────
PALM = """
<g id="palm">
  <path d="M0 0 C 6 -60 14 -130 34 -205 L 42 -204 C 24 -128 16 -58 12 0 Z"/>
  <g transform="translate(38 -206)">
    <path d="M0 0 C -30 -30 -80 -30 -118 4 C -80 -14 -40 -12 0 6 Z"/>
    <path d="M0 0 C -20 -44 -60 -66 -96 -60 C -60 -48 -30 -30 -2 8 Z"/>
    <path d="M0 0 C 10 -46 42 -74 82 -76 C 50 -56 26 -30 4 8 Z"/>
    <path d="M0 0 C 36 -24 84 -22 120 10 C 82 -4 40 -2 2 8 Z"/>
    <path d="M0 0 C 30 6 64 30 80 64 C 56 40 28 26 -2 8 Z"/>
    <path d="M0 0 C -26 8 -58 34 -70 70 C -50 44 -24 28 2 8 Z"/>
  </g>
</g>"""


def hero() -> None:
    w, h = 1280, 560
    title, by, tag = "CLIPPER", "BY NUMAN HAYDER", "CLIPS GTA 6 STREAMERS WHILE THEY'RE STILL LIVE"
    css = (
        font_face("Anton", "Anton-Regular.ttf", "GTA6" + title + "VI")
        + font_face("Bebas", "BebasNeue-Regular.ttf", by + tag + "● REC LIVE 36 STREAMERS TWITCH YOUTUBE·")
        + f"""
.sun{{animation:glow 4s ease-in-out infinite alternate;transform-origin:640px 300px}}
@keyframes glow{{from{{filter:drop-shadow(0 0 18px {PINK}88)}}to{{filter:drop-shadow(0 0 46px {ORANGE}cc)}}}}
.waves{{animation:waves 3s linear infinite}}
@keyframes waves{{from{{transform:translateY(0)}}to{{transform:translateY(24px)}}}}
.swayL{{transform-box:fill-box;transform-origin:0% 100%;animation:sway 5s ease-in-out infinite alternate}}
.swayR{{transform-box:fill-box;transform-origin:0% 100%;animation:sway 6s ease-in-out -2s infinite alternate}}
@keyframes sway{{from{{transform:rotate(-1.6deg)}}to{{transform:rotate(1.8deg)}}}}
.in1{{animation:rise .9s cubic-bezier(.2,.9,.2,1.2) .15s both}}
.in2{{animation:rise .9s cubic-bezier(.2,.9,.2,1.2) .45s both}}
.in3{{animation:rise .9s cubic-bezier(.2,.9,.2,1) .75s both}}
.in4{{animation:fade 1s ease 1.1s both}}
@keyframes rise{{from{{opacity:0;transform:translateY(40px) scale(.96)}}to{{opacity:1;transform:none}}}}
@keyframes fade{{from{{opacity:0}}to{{opacity:1}}}}
.shine{{animation:shine 4.5s ease-in-out 1.6s infinite}}
@keyframes shine{{0%{{transform:translateX(-500px)}}45%,100%{{transform:translateX(1400px)}}}}
.rec{{animation:blink 1.2s steps(1) infinite}}
@keyframes blink{{50%{{opacity:.15}}}}
.stars circle{{animation:twinkle 3s ease-in-out infinite alternate}}
.stars circle:nth-child(2n){{animation-delay:-1.4s}}
.stars circle:nth-child(3n){{animation-delay:-2.2s}}
@keyframes twinkle{{from{{opacity:.15}}to{{opacity:.9}}}}
.scan{{animation:scan 7s linear infinite}}
@keyframes scan{{from{{transform:translateY(-80px)}}to{{transform:translateY({h}px)}}}}
{REDUCED}"""
    )
    stars = "".join(
        f'<circle cx="{x}" cy="{y}" r="{r}" fill="#fff"/>'
        for x, y, r in [(90, 60, 1.4), (210, 120, 1), (330, 40, 1.2), (470, 90, .9), (820, 50, 1.3), (930, 110, 1),
                        (1060, 70, 1.5), (1180, 130, 1), (150, 190, .8), (1120, 200, .9), (720, 30, 1), (560, 160, .8)]
    )
    cuts = "".join(f'<rect x="0" y="{y}" width="{w}" height="{hh}" fill="#000"/>'
                   for y, hh in [(300, 6), (322, 9), (346, 12), (372, 15)])
    waves = "".join(
        f'<rect x="0" y="{404 + i * 24}" width="{w}" height="{2 + i * .5}" fill="{PINK}" opacity="{.55 - i * .06:.2f}"/>'
        for i in range(-1, 7)
    )
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="GTA 6 Clipper by Numan Hayder: clips GTA 6 streamers while they are still live">
<style>{css}</style>
<defs>
  <linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#12081f"/><stop offset=".45" stop-color="#3a1047"/>
    <stop offset=".7" stop-color="#b8306f"/><stop offset=".74" stop-color="{ORANGE}"/>
  </linearGradient>
  <linearGradient id="sun" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="{GOLD}"/><stop offset=".5" stop-color="{ORANGE}"/><stop offset="1" stop-color="{PINK}"/>
  </linearGradient>
  <linearGradient id="sea" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#2a0c35"/><stop offset="1" stop-color="{NIGHT}"/>
  </linearGradient>
  <linearGradient id="ttl" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#fff"/><stop offset=".55" stop-color="#ffe3f1"/><stop offset="1" stop-color="{PINK}"/>
  </linearGradient>
  <linearGradient id="beam" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset=".5" stop-color="#fff" stop-opacity=".85"/><stop offset="1" stop-color="#fff" stop-opacity="0"/>
  </linearGradient>
  <linearGradient id="scanG" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset="1" stop-color="#fff" stop-opacity=".05"/>
  </linearGradient>
  <mask id="cuts"><rect width="{w}" height="{h}" fill="#fff"/>{cuts}</mask>
  <clipPath id="sky-clip"><rect width="{w}" height="402"/></clipPath>
  <clipPath id="sea-clip"><rect y="402" width="{w}" height="{h - 402}"/></clipPath>
  <clipPath id="title-clip"><text x="640" y="300" text-anchor="middle" font-family="Anton" font-size="168" transform="skewX(-8) translate(42 0)">GTA 6 {title}</text></clipPath>
  <clipPath id="frame"><rect width="{w}" height="{h}" rx="22"/></clipPath>
  {PALM}
</defs>
<g clip-path="url(#frame)">
  <rect width="{w}" height="{h}" fill="url(#sky)"/>
  <g class="stars">{stars}</g>
  <g clip-path="url(#sky-clip)"><circle class="sun" cx="640" cy="330" r="190" fill="url(#sun)" mask="url(#cuts)"/></g>
  <rect y="402" width="{w}" height="{h - 402}" fill="url(#sea)"/>
  <g clip-path="url(#sea-clip)"><g class="waves">{waves}</g></g>
  <rect y="400" width="{w}" height="3" fill="{PINK}"/>
  <g fill="{NIGHT}">
    <g class="swayL"><use xlink:href="#palm" href="#palm" transform="translate(70 430) scale(1.15)"/></g>
    <g class="swayR"><use xlink:href="#palm" href="#palm" transform="translate(150 440) scale(-.8 .8)"/></g>
    <g class="swayR"><use xlink:href="#palm" href="#palm" transform="translate(1210 436) scale(-1.2 1.2)"/></g>
    <g class="swayL"><use xlink:href="#palm" href="#palm" transform="translate(1110 444) scale(.75)"/></g>
  </g>

  <g class="in1">
    <text x="640" y="300" text-anchor="middle" font-family="Anton" font-size="168" fill="{NIGHT}" opacity=".55" transform="skewX(-8) translate(50 10)">GTA 6 {title}</text>
    <text x="640" y="300" text-anchor="middle" font-family="Anton" font-size="168" fill="url(#ttl)" stroke="{NIGHT}" stroke-width="3" paint-order="stroke" transform="skewX(-8) translate(42 0)">GTA 6 {title}</text>
    <g clip-path="url(#title-clip)"><rect class="shine" x="0" y="100" width="220" height="260" fill="url(#beam)" transform="skewX(-20)"/></g>
  </g>
  <text class="in2" x="640" y="352" text-anchor="middle" font-family="Bebas" font-size="40" letter-spacing="10" fill="#fff" stroke="{NIGHT}" stroke-width="6" paint-order="stroke">{by}</text>
  <g class="in3">
    <rect x="300" y="474" width="680" height="48" rx="24" fill="{NIGHT}" opacity=".78" stroke="{PINK}" stroke-opacity=".5"/>
    <text x="640" y="507" text-anchor="middle" font-family="Bebas" font-size="28" letter-spacing="3" fill="#fff">{esc(tag)}</text>
  </g>
  <g class="in4">
    <rect x="34" y="30" width="164" height="44" rx="10" fill="{NIGHT}" opacity=".8"/>
    <circle class="rec" cx="60" cy="52" r="9" fill="#ff2d55"/>
    <text x="78" y="62" font-family="Bebas" font-size="30" letter-spacing="2" fill="#fff">REC LIVE</text>
    <rect x="902" y="30" width="344" height="44" rx="10" fill="{NIGHT}" opacity=".8"/>
    <text x="1074" y="62" text-anchor="middle" font-family="Bebas" font-size="28" letter-spacing="2" fill="#fff">36 STREAMERS · TWITCH · YOUTUBE</text>
  </g>
  <rect class="scan" x="0" y="0" width="{w}" height="80" fill="url(#scanG)"/>
</g>
</svg>"""
    write("hero.svg", svg)


def terminal() -> None:
    w = 1000
    lines = [
        ("cmd", "git clone https://github.com/byte4day/gta6-clipper && cd gta6-clipper"),
        ("cmd", "./gtaclip-run run"),
        ("dim", "building Rust core… done"),
        ("ok", "dashboard → http://127.0.0.1:8787"),
        ("dim", "gtacore live: 36 streamers checked in 1.1 s · 3 live"),
        ("rec", "● REC summit1g   720p60 · headless browser · ads skipped"),
        ("cut", "✂ chat_spike: chat 41/s (normal 6.1/s, hype 64%)"),
        ("ok", "✓ clip ready · 9:16 · word-by-word captions · −14 LUFS"),
    ]
    colors = {"cmd": "#ffffff", "dim": "#9b8fb0", "ok": "#5df2a0", "rec": "#ff4d6d", "cut": GOLD}
    top, lh = 74, 36
    h = top + lh * len(lines) + 40
    cycle = 14.0
    # each line appears at time t; commands are typed (clip grows), output pops in
    t, rows, keys = 0.6, [], []
    for i, (kind, text) in enumerate(lines):
        y = top + i * lh
        prefix = "$ " if kind == "cmd" else ""
        full = prefix + text
        width = len(full) * 10.85 + 6
        dur = len(text) * 0.035 if kind == "cmd" else 0.12
        a, b = t / cycle * 100, (t + dur) / cycle * 100
        anim = f"l{i}"
        if kind == "cmd":
            n = len(text)
            keys.append(
                f"@keyframes {anim}{{0%,{a:.2f}%{{transform:scaleX(0);animation-timing-function:steps({n},end)}}"
                f"{b:.2f}%,93%{{transform:scaleX(1)}}97%,100%{{transform:scaleX(0)}}}}"
            )
            rows.append(
                f'<clipPath id="c{i}"><rect x="50" y="{y - 26}" width="{width:.0f}" height="36" '
                f'style="transform-box:fill-box;transform-origin:left;animation:{anim} {cycle}s linear infinite"/></clipPath>'
                f'<text x="32" y="{y}" fill="{PINK}">$</text>'
                f'<text x="54" y="{y}" fill="#fff" clip-path="url(#c{i})">{esc(text)}</text>'
            )
            t += dur + 0.5
        else:
            keys.append(f"@keyframes {anim}{{0%,{a:.2f}%{{opacity:0;transform:translateX(-8px)}}{b:.2f}%,93%{{opacity:1;transform:none}}97%,100%{{opacity:0}}}}")
            rows.append(f'<text x="32" y="{y}" fill="{colors[kind]}" style="animation:{anim} {cycle}s ease-out infinite">{esc(text)}</text>')
            t += 0.55 if kind != "ok" else 0.8
    cursor_y = top + (len(lines)) * lh
    css = (
        font_face("Bebas", "BebasNeue-Regular.ttf", "MISSION START GTA6 CLIPPER")
        + "".join(keys)
        + f"""
text{{font-family:{MONO};font-size:18px}}
.cur{{animation:blink 1s steps(1) infinite}}
@keyframes blink{{50%{{opacity:0}}}}
.glow{{animation:glow 3s ease-in-out infinite alternate}}
@keyframes glow{{from{{stroke-opacity:.35}}to{{stroke-opacity:.95}}}}
{REDUCED}"""
    )
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="Start: git clone https://github.com/byte4day/gta6-clipper, cd gta6-clipper, ./gtaclip-run run">
<style>{css}</style>
<defs><linearGradient id="bar" x1="0" x2="1"><stop offset="0" stop-color="{PINK}"/><stop offset="1" stop-color="{ORANGE}"/></linearGradient></defs>
<rect x="1" y="1" width="{w - 2}" height="{h - 2}" rx="16" fill="#120b1c" stroke="url(#bar)" stroke-width="2" class="glow"/>
<circle cx="28" cy="26" r="7" fill="#ff5f57"/><circle cx="50" cy="26" r="7" fill="#febc2e"/><circle cx="72" cy="26" r="7" fill="#28c840"/>
<text x="{w / 2}" y="33" text-anchor="middle" font-family="Bebas" style="font-family:Bebas;font-size:22px;letter-spacing:4px" fill="#cdbfe0">MISSION START · GTA6 CLIPPER</text>
<rect x="0" y="44" width="{w}" height="1" fill="#2a1d3b"/>
{''.join(rows)}
<rect class="cur" x="32" y="{cursor_y - 20}" width="11" height="22" fill="{PINK}"/>
</svg>"""
    write("start.svg", svg)


def header(name: str, num: str, title: str, sub: str) -> None:
    w, h = 1000, 120
    css = (
        font_face("Anton", "Anton-Regular.ttf", title.upper())
        + font_face("Bebas", "BebasNeue-Regular.ttf", f"MISSION {num} " + sub.upper())
        + f"""
.bar{{transform-box:fill-box;transform-origin:left;animation:wipe 1s cubic-bezier(.7,0,.2,1) both}}
@keyframes wipe{{from{{transform:scaleX(0)}}to{{transform:scaleX(1)}}}}
.t{{animation:slide .8s cubic-bezier(.2,.9,.2,1.15) .25s both}}
.s{{animation:fadein .8s ease .55s both}}
@keyframes slide{{from{{opacity:0;transform:translateX(-60px)}}to{{opacity:1;transform:none}}}}
@keyframes fadein{{from{{opacity:0}}to{{opacity:1}}}}
.sweep{{animation:sweep 3.6s ease-in-out 1.2s infinite}}
@keyframes sweep{{0%{{transform:translateX(-200px)}}60%,100%{{transform:translateX({w + 200}px)}}}}
.dot{{animation:pulse 1.6s ease-in-out infinite}}
@keyframes pulse{{50%{{opacity:.25}}}}
{REDUCED}"""
    )
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="{esc(title)}">
<style>{css}</style>
<defs>
  <linearGradient id="g" x1="0" x2="1"><stop offset="0" stop-color="{PINK}"/><stop offset=".6" stop-color="{ORANGE}"/><stop offset="1" stop-color="{ORANGE}" stop-opacity="0"/></linearGradient>
  <linearGradient id="sw" x1="0" x2="1"><stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset=".5" stop-color="#fff" stop-opacity=".9"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>
  <clipPath id="barc"><rect x="0" y="104" width="{w}" height="6"/></clipPath>
</defs>
<g class="s"><circle class="dot" cx="9" cy="20" r="6" fill="{PINK}"/>
<text x="24" y="28" font-family="Bebas" font-size="24" letter-spacing="5" fill="{ORANGE}">MISSION {num} · {esc(sub.upper())}</text></g>
<g class="t" transform="skewX(-8)">
  <text x="12" y="92" font-family="Anton" font-size="62" fill="#000" opacity=".35" transform="translate(4 4)">{esc(title.upper())}</text>
  <text x="12" y="92" font-family="Anton" font-size="62" fill="#fff" stroke="#120b1c" stroke-width="2" paint-order="stroke">{esc(title.upper())}</text>
</g>
<rect class="bar" x="0" y="104" width="{w}" height="6" rx="3" fill="url(#g)"/>
<g clip-path="url(#barc)"><rect class="sweep" x="0" y="104" width="160" height="6" fill="url(#sw)"/></g>
</svg>"""
    write(f"h-{name}.svg", svg)


def passed() -> None:
    w, h = 1000, 220
    css = (
        font_face("Anton", "Anton-Regular.ttf", "MISSION PASSED!")
        + font_face("Bebas", "BebasNeue-Regular.ttf", "RESPECT + CLIPS READY BEFORE THE STREAM ENDS")
        + f"""
.mp{{transform-box:fill-box;transform-origin:center;animation:pop 6s cubic-bezier(.2,1.4,.3,1) infinite}}
@keyframes pop{{0%{{opacity:0;transform:scale(1.8)}}10%,80%{{opacity:1;transform:scale(1)}}92%,100%{{opacity:0;transform:scale(1)}}}}
.rs{{animation:rs 6s ease infinite}}
@keyframes rs{{0%,14%{{opacity:0;transform:translateY(16px)}}22%,80%{{opacity:1;transform:none}}92%,100%{{opacity:0}}}}
.band{{transform-box:fill-box;transform-origin:center;animation:band 6s ease infinite}}
@keyframes band{{0%{{transform:scaleY(0)}}6%,86%{{transform:scaleY(1)}}96%,100%{{transform:scaleY(0)}}}}
{REDUCED}"""
    )
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="Mission passed! Respect plus. Clips ready before the stream ends.">
<style>{css}</style>
<defs><linearGradient id="band" x1="0" x2="1"><stop offset="0" stop-color="#000" stop-opacity="0"/><stop offset=".2" stop-color="#000" stop-opacity=".72"/><stop offset=".8" stop-color="#000" stop-opacity=".72"/><stop offset="1" stop-color="#000" stop-opacity="0"/></linearGradient>
<linearGradient id="gold" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffe680"/><stop offset=".6" stop-color="{GOLD}"/><stop offset="1" stop-color="#e09a1a"/></linearGradient></defs>
<rect class="band" x="0" y="30" width="{w}" height="160" fill="url(#band)"/>
<text class="mp" x="{w / 2}" y="122" text-anchor="middle" font-family="Anton" font-size="84" fill="url(#gold)" stroke="#000" stroke-width="5" paint-order="stroke">MISSION PASSED!</text>
<g class="rs"><text x="{w / 2}" y="168" text-anchor="middle" font-family="Bebas" font-size="34" letter-spacing="4" fill="#fff" stroke="#000" stroke-width="3" paint-order="stroke">RESPECT +   ·   CLIPS READY BEFORE THE STREAM ENDS</text></g>
</svg>"""
    write("passed.svg", svg)


def shots() -> None:
    from PIL import Image

    for src, name in [("top", "dashboard"), ("clips", "clips"), ("editor", "editor"), ("settings", "settings")]:
        p = CAPTURES / f"{src}.png"
        if not p.exists():
            continue
        im = Image.open(p).convert("RGB")
        im = im.resize((1800, round(im.height * 1800 / im.width)), Image.LANCZOS)
        im.save(HERE / f"{name}.webp", "WEBP", quality=88, method=6)
        print(f"{name}.webp".ljust(22), f"{(HERE / f'{name}.webp').stat().st_size / 1024:6.1f} KB")


if __name__ == "__main__":
    hero()
    terminal()
    for args in [
        ("start", "01", "Start in one command", "get in the car"),
        ("clips", "02", "What it clips", "six ways to catch the moment"),
        ("dash", "03", "The dashboard", "your hideout"),
        ("how", "04", "How it works", "the heist plan"),
        ("rust", "05", "The Rust core", "faster getaway"),
        ("config", "06", "Settings and output", "tune the ride"),
    ]:
        header(*args)
    passed()
    shots()
