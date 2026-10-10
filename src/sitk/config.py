from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
SESSION_DIR = Path.home() / ".config" / "stayintheknow"
CONFIG_PATH = ROOT / "config.toml"
ENV_PATH = ROOT / ".env"


@dataclass
class WatchTarget:
    handle: str
    followers: bool = True
    following: bool = True
    report_removals: bool = True

    def kinds(self) -> list[str]:
        return [k for k, on in (("followers", self.followers), ("following", self.following)) if on]


@dataclass
class Schedule:
    relations_hours: float = 3
    full_refresh_hours: float = 24
    posts_hours: float = 3
    jitter: float = 0.2
    quiet_hours: tuple[int, int] | None = (1, 7)
    heartbeat_hour: int = 9


@dataclass
class Config:
    session_user: str = ""
    watch: list[WatchTarget] = field(default_factory=list)
    event_pages: list[str] = field(default_factory=list)
    schedule: Schedule = field(default_factory=Schedule)
    discord_webhook_url: str = ""
    discord_user_id: str = ""
    healthcheck_url: str = ""


def load_env(path: Path = ENV_PATH) -> dict[str, str]:
    """Minimal .env parser. Real environment variables win over the file."""
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key in list(values):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def load_config(path: Path = CONFIG_PATH, env_path: Path = ENV_PATH) -> Config:
    raw = tomllib.loads(path.read_text()) if path.exists() else {}
    env = load_env(env_path)

    sched_raw = dict(raw.get("schedule", {}))
    quiet = sched_raw.pop("quiet_hours", [1, 7])
    schedule = Schedule(**sched_raw, quiet_hours=tuple(quiet) if quiet else None)

    return Config(
        session_user=raw.get("instagram", {}).get("session_user", "").lstrip("@"),
        watch=[WatchTarget(**{**w, "handle": w["handle"].lstrip("@")}) for w in raw.get("watch", [])],
        event_pages=[h.lstrip("@") for h in raw.get("instagram", {}).get("event_pages", [])],
        schedule=schedule,
        discord_webhook_url=env.get("DISCORD_WEBHOOK_URL", ""),
        discord_user_id=env.get("DISCORD_USER_ID", ""),
        healthcheck_url=env.get("HEALTHCHECK_URL", ""),
    )
