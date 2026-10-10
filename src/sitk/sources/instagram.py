"""Instagram access via instaloader, using a session imported from the owner's browser."""
from __future__ import annotations

import itertools
import os
from contextlib import contextmanager
from datetime import timezone
from pathlib import Path

import instaloader
from instaloader import exceptions as ix

from .base import AuthError, PostInfo, ProfileInfo, RateLimited, SourceError

AUTH_HINTS = ("checkpoint", "challenge", "login", "401", "logged in", "csrf")
RATE_HINTS = ("429", "please wait", "too many", "rate")


def session_path(session_dir: Path, username: str) -> Path:
    return session_dir / f"session-{username}"


def new_loader() -> instaloader.Instaloader:
    return instaloader.Instaloader(
        quiet=True,
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        save_metadata=False,
        compress_json=False,
        max_connection_attempts=2,
        request_timeout=60,
    )


def save_session(loader: instaloader.Instaloader, session_dir: Path, username: str) -> Path:
    session_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(session_dir, 0o700)
    path = session_path(session_dir, username)
    loader.save_session_to_file(str(path))
    os.chmod(path, 0o600)
    return path


@contextmanager
def translate_errors():
    try:
        yield
    except (ix.LoginRequiredException, ix.LoginException) as e:
        raise AuthError(str(e)) from e
    except ix.TooManyRequestsException as e:
        raise RateLimited(str(e)) from e
    except ix.PrivateProfileNotFollowedException as e:
        raise SourceError(f"private profile not followed by the session account: {e}") from e
    except ix.ProfileNotExistsException as e:
        raise SourceError(f"profile does not exist: {e}") from e
    except (ix.QueryReturnedForbiddenException, ix.ConnectionException, ix.BadResponseException) as e:
        text = str(e).lower()
        if any(h in text for h in RATE_HINTS):
            raise RateLimited(str(e)) from e
        if any(h in text for h in AUTH_HINTS):
            raise AuthError(str(e)) from e
        raise SourceError(str(e)) from e


class InstagramSource:
    def __init__(self, session_user: str, session_dir: Path):
        if not session_user:
            raise AuthError("no session_user configured; run `sitk login`")
        path = session_path(session_dir, session_user)
        if not path.exists():
            raise AuthError(f"no saved session for @{session_user}; run `sitk login`")
        self.loader = new_loader()
        with translate_errors():
            self.loader.load_session_from_file(session_user, str(path))
        self.session_user = session_user

    def whoami(self) -> str:
        with translate_errors():
            user = self.loader.test_login()
        if not user:
            raise AuthError("session is no longer logged in")
        return user

    def _profile(self, handle: str) -> instaloader.Profile:
        with translate_errors():
            return instaloader.Profile.from_username(self.loader.context, handle)

    def profile(self, handle: str) -> ProfileInfo:
        p = self._profile(handle)
        with translate_errors():
            is_self = p.username.lower() == self.session_user.lower()
            return ProfileInfo(
                user_id=str(p.userid),
                username=p.username,
                followers=p.followers,
                following=p.followees,
                is_private=p.is_private,
                viewer_can_see=(not p.is_private) or is_self or p.followed_by_viewer,
            )

    def relations(self, handle: str, kind: str) -> dict[str, str]:
        p = self._profile(handle)
        with translate_errors():
            it = p.get_followers() if kind == "followers" else p.get_followees()
            return {str(u.userid): u.username for u in it}

    def recent_posts(self, handle: str, limit: int = 12) -> list[PostInfo]:
        p = self._profile(handle)
        with translate_errors():
            return [
                PostInfo(
                    post_id=str(post.mediaid),
                    account=p.username,
                    shortcode=post.shortcode,
                    caption=post.caption or "",
                    posted_at=post.date_utc.replace(tzinfo=timezone.utc),
                    media_url=post.url,
                )
                for post in itertools.islice(p.get_posts(), limit)
            ]
