"""Config + .env loading."""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONTS_DIR = ROOT / "assets" / "fonts"
ENV_FILE = ROOT / ".env"
SETTINGS_FILE = ROOT / "state" / "settings.json"  # changes made from the dashboard (override config.toml)


def load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip() if not line.strip().startswith("#") else ""
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and not os.environ.get(k):
            os.environ[k] = v


@dataclass
class Streamer:
    platform: str
    name: str
    enabled: bool = True

    @property
    def key(self) -> str:
        return self.name.lstrip("@").lower()

    @property
    def url(self) -> str:
        if self.platform == "youtube":
            handle = self.name if self.name.startswith(("@", "UC")) else "@" + self.name
            base = "channel/" + handle if handle.startswith("UC") else handle
            return f"https://www.youtube.com/{base}/live"
        return f"https://www.twitch.tv/{self.name.lower()}"

    @property
    def display(self) -> str:
        return self.name.lstrip("@")


def set_env_key(name: str, value: str) -> None:
    """Write/replace/remove NAME=value in .env and apply it to this process immediately."""
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    lines = [l for l in lines if not l.strip().startswith(f"{name}=")]
    if value:
        lines.append(f"{name}={value}")
        os.environ[name] = value
    else:
        os.environ.pop(name, None)
    ENV_FILE.write_text("\n".join(lines) + "\n")
    ENV_FILE.chmod(0o600)


@dataclass
class Config:
    raw: dict
    streamers: list[Streamer] = field(default_factory=list)
    overrides: dict = field(default_factory=dict)

    def set(self, section: str, key: str, value) -> None:
        self.raw.setdefault(section, {})[key] = value
        self.overrides.setdefault(section, {})[key] = value
        self.save()

    def save(self) -> None:
        SETTINGS_FILE.parent.mkdir(exist_ok=True)
        data = {**self.overrides,
                "streamers": [{"platform": s.platform, "name": s.name, "enabled": s.enabled} for s in self.streamers]}
        SETTINGS_FILE.write_text(json.dumps(data, indent=2))

    def __getitem__(self, section: str) -> dict:
        return self.raw.get(section, {})

    def path(self, key: str) -> Path:
        p = Path(self["general"].get(key, key))
        return p if p.is_absolute() else ROOT / p

    def pre_post(self, trigger: str) -> tuple[float, float]:
        pp = self["pre_post"].get(trigger, [25, 8])
        return float(pp[0]), float(pp[1])


def load_config(path: str | Path | None = None) -> Config:
    load_env()
    path = Path(path) if path else ROOT / "config.toml"
    raw = tomllib.loads(path.read_text())
    overrides = json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.exists() else {}
    for section, vals in overrides.items():
        if section != "streamers" and isinstance(vals, dict):
            raw.setdefault(section, {}).update(vals)
    src = overrides.get("streamers", raw.get("streamers", []))
    streamers = [Streamer(s.get("platform", "twitch").lower(), s["name"], s.get("enabled", True)) for s in src]
    return Config(raw, streamers, {k: v for k, v in overrides.items() if k != "streamers"})
