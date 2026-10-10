"""Instagram access via instaloader, using a session imported from the owner's browser."""
from __future__ import annotations

import itertools
from contextlib import contextmanager
from datetime import timezone
from pathlib import Path

import instaloader
from instaloader import exceptions as ix

from ..cookies import BrowserCookie, describe_session, load_session_file, redact, to_cookiejar
from .base import AuthError, PostInfo, ProfileInfo, RateLimited, SourceError

AUTH_HINTS = ("checkpoint", "challenge", "login", "401", "logged in", "logged out", "csrf")
# Instagram flagged the account/session as automated. Treat like an auth problem:
# stop all requests until the owner decides what to do.
BLOCK_HINTS = ("feedback_required", "checkpoint_required", "challenge_required", "consent_required")
RATE_HINTS = ("429", "please wait", "too many", "rate")


def session_path(session_dir: Path, username: str) -> Path:
    return session_dir / f"session-{username}"


def new_loader(user_agent: str) -> instaloader.Instaloader:
    return instaloader.Instaloader(
        quiet=True,
        user_agent=user_agent,  # must be set before the session is built; headers are derived from it
        iphone_support=False,   # otherwise some requests go out with an iPhone-app UA
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        save_metadata=False,
        compress_json=False,
        max_connection_attempts=1,
        request_timeout=60,
    )


@contextmanager
def translate_errors():
    try:
        yield
    except ix.AbortDownloadException as e:
        # Not an InstaloaderException subclass; raised on blocks and forced logouts.
        text = str(e).lower()
        if any(h in text for h in RATE_HINTS) and not any(h in text for h in BLOCK_HINTS):
            raise RateLimited(str(e)) from e
        raise AuthError(f"Instagram blocked or logged out the session: {e}") from e
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
        if any(h in text for h in BLOCK_HINTS):
            raise AuthError(f"Instagram blocked the session: {e}") from e
        if any(h in text for h in RATE_HINTS):
            raise RateLimited(str(e)) from e
        if any(h in text for h in AUTH_HINTS):
            raise AuthError(str(e)) from e
        raise SourceError(str(e)) from e


def build_loader(username: str, cookies: list[BrowserCookie], user_agent: str) -> instaloader.Instaloader:
    """An instaloader whose requests session carries the browser cookies with their
    original domain/path/expiry/secure/HttpOnly/SameSite and the browser's UA."""
    missing = {"sessionid", "csrftoken"} - {c.name for c in cookies}
    if missing:
        raise ValueError(f"session has no {', '.join(sorted(missing))} cookie; log into Instagram in the browser "
                         "and re-run `sitk login`")
    loader = new_loader(user_agent)
    jar = to_cookiejar(cookies)
    # load_session builds the session headers (UA, X-CSRFToken); then swap in the
    # attribute-preserving jar instead of instaloader's name/value-only one.
    loader.context.load_session(username, {c.name: c.value for c in cookies})
    loader.context._session.cookies = jar  # noqa: SLF001
    return loader


class InstagramSource:
    def __init__(self, session_user: str, session_dir: Path, user_agent_override: str = ""):
        if not session_user:
            raise AuthError("no session_user configured; run `sitk login`")
        path = session_path(session_dir, session_user)
        if not path.exists():
            raise AuthError(f"no saved session for @{session_user}; run `sitk login`")
        try:
            data = load_session_file(path)
            self.user_agent = user_agent_override or data["user_agent"]
            self.loader = build_loader(session_user, data["cookies"], self.user_agent)
        except ValueError as e:
            raise AuthError(str(e)) from e
        self.session_user = session_user
        self.session = self.loader.context._session  # noqa: SLF001
        self.last_response: tuple[int, str, str] | None = None
        self.session.hooks["response"].append(self._record)

    def _record(self, resp, *args, **kwargs):
        # Passive: remembers what Instagram answered, for `sitk doctor`. Adds no requests.
        if "instagram.com" in resp.url:
            body = " ".join((resp.text or "")[:500].split())
            self.last_response = (resp.status_code, resp.url.split("?")[0], body)
        return resp

    def diagnostics(self) -> list[str]:
        last = None
        if self.last_response:
            status, url, body = self.last_response
            last = (status, url, redact(body, self.session.cookies))
        return describe_session(self.session.cookies, self.user_agent, last)

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
