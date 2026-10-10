"""Discord webhook delivery and message formatting."""
from __future__ import annotations

import logging
import time
from datetime import datetime

import httpx

from .diff import Change

log = logging.getLogger(__name__)

GREEN, RED, BLUE, ORANGE, GREY = 0x2ECC71, 0xE74C3C, 0x3498DB, 0xE67E22, 0x95A5A6
MAX_NAMES = 25
EMBEDS_PER_MESSAGE = 10


def ig_link(username: str) -> str:
    return f"[@{username}](https://www.instagram.com/{username}/)"


def name_list(changes: list[Change]) -> str:
    names = [ig_link(c.username) for c in changes[:MAX_NAMES]]
    extra = len(changes) - MAX_NAMES
    return "\n".join(names) + (f"\n…and {extra} more" if extra > 0 else "")


TITLES = {
    ("followers", "added"): ("New follower{s} on @{account}", GREEN),
    ("followers", "removed"): ("@{account} lost {n} follower{s}", RED),
    ("following", "added"): ("@{account} started following {n} account{s}", BLUE),
    ("following", "removed"): ("@{account} unfollowed {n} account{s}", ORANGE),
}


def relation_embeds(account: str, kind: str, changes: list[Change]) -> list[dict]:
    embeds = []
    for change in ("added", "removed"):
        group = [c for c in changes if c.change == change]
        if not group:
            continue
        title, color = TITLES[(kind, change)]
        n = len(group)
        embeds.append({
            "title": title.format(account=account, n=n, s="" if n == 1 else "s"),
            "description": name_list(group),
            "color": color,
        })
    return embeds


def post_embed(post, info) -> dict:
    caption = (post.caption or "").strip()
    if len(caption) > 1500:
        caption = caption[:1500] + "…"
    embed = {
        "title": f"{'📅 Event' if info.is_event else 'New post'} from @{post.account}",
        "url": post.permalink,
        "description": caption or "(no caption)",
        "color": ORANGE if info.is_event else GREY,
        "timestamp": post.posted_at.isoformat(),
        "image": {"url": post.media_url} if post.media_url else None,
        "fields": [],
    }
    if info.is_event:
        if info.when:
            when = datetime.fromisoformat(info.when)
            embed["fields"].append({"name": "When", "value": when.strftime("%a %b %-d, %-I:%M %p").replace(":00 ", " "), "inline": True})
        if info.venue:
            embed["fields"].append({"name": "Where", "value": info.venue, "inline": True})
        if info.price:
            embed["fields"].append({"name": "Price", "value": info.price, "inline": True})
    return {k: v for k, v in embed.items() if v not in (None, [])}


class Discord:
    def __init__(self, webhook_url: str, user_id: str = "", client: httpx.Client | None = None):
        if not webhook_url.startswith("https://discord.com/api/webhooks/"):
            raise ValueError("DISCORD_WEBHOOK_URL is missing or not a Discord webhook URL")
        self.url = webhook_url
        self.user_id = user_id
        self.client = client or httpx.Client(timeout=20)

    def send(self, content: str = "", embeds: list[dict] | None = None, ping: bool = False) -> None:
        embeds = embeds or []
        chunks = [embeds[i:i + EMBEDS_PER_MESSAGE] for i in range(0, len(embeds), EMBEDS_PER_MESSAGE)] or [[]]
        for i, chunk in enumerate(chunks):
            text = content if i == 0 else ""
            mention = ping and i == 0 and self.user_id
            if mention:
                text = f"<@{self.user_id}> {text}".strip()
            self._post({
                "username": "Stay In The Know",
                "content": text,
                "embeds": chunk,
                # Only ever allow pinging the owner; captions can't trigger @everyone.
                "allowed_mentions": {"users": [self.user_id]} if mention else {"parse": []},
            })

    def _post(self, payload: dict, attempts: int = 4) -> None:
        for _ in range(attempts):
            resp = self.client.post(self.url, json=payload, params={"wait": "true"})
            if resp.status_code == 429:
                retry = float(resp.json().get("retry_after", 1))
                log.warning("Discord rate limited, retrying in %.1fs", retry)
                time.sleep(min(retry, 30))
                continue
            if resp.status_code >= 400:
                # Never include the webhook URL in errors/logs.
                raise RuntimeError(f"Discord webhook failed: HTTP {resp.status_code} {resp.text[:200]}")
            return
        raise RuntimeError("Discord webhook kept rate limiting")
