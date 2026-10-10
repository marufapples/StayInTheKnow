from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class AuthError(Exception):
    """Session missing, expired, or challenged. Requires the owner to log in again."""


class RateLimited(Exception):
    """Instagram asked us to slow down. Back off, do not retry immediately."""


class SourceError(Exception):
    """Anything else: profile missing, private and not followed, network trouble."""


@dataclass(frozen=True)
class ProfileInfo:
    user_id: str
    username: str
    followers: int
    following: int
    is_private: bool
    viewer_can_see: bool  # viewer is this account or follows it (or it is public)


@dataclass(frozen=True)
class PostInfo:
    post_id: str
    account: str
    shortcode: str
    caption: str
    posted_at: datetime
    media_url: str

    @property
    def permalink(self) -> str:
        return f"https://www.instagram.com/p/{self.shortcode}/"


class Source(Protocol):
    def whoami(self) -> str: ...
    def profile(self, handle: str) -> ProfileInfo: ...
    def relations(self, handle: str, kind: str) -> dict[str, str]: ...
    def recent_posts(self, handle: str, limit: int = 12) -> list[PostInfo]: ...
