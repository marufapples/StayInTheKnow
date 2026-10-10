import random
from datetime import datetime, timedelta, timezone

import pytest

from sitk.app import App
from sitk.config import Config, Schedule, WatchTarget
from sitk.sources.base import AuthError, PostInfo, ProfileInfo, RateLimited
from sitk.store import Store

T0 = datetime(2026, 10, 10, 14, 0, tzinfo=timezone.utc)


class FakeSource:
    def __init__(self):
        self.followers = {"1": "alice", "2": "bob"}
        self.following = {"9": "venue"}
        self.reported_followers = None
        self.posts = []
        self.calls = []
        self.error = None

    def whoami(self):
        return "me"

    def profile(self, handle):
        self.calls.append(("profile", handle))
        if self.error:
            raise self.error
        reported = self.reported_followers if self.reported_followers is not None else len(self.followers)
        return ProfileInfo("100", handle, reported, len(self.following), True, True)

    def relations(self, handle, kind):
        self.calls.append((kind, handle))
        return dict(self.followers if kind == "followers" else self.following)

    def recent_posts(self, handle, limit=12):
        self.calls.append(("posts", handle))
        return list(self.posts)


class FakeDiscord:
    def __init__(self):
        self.sent = []
        self.fail = False

    def send(self, content="", embeds=None, ping=False):
        if self.fail:
            raise RuntimeError("discord down")
        self.sent.append({"content": content, "embeds": embeds or [], "ping": ping})


@pytest.fixture
def env():
    clock = {"now": T0}
    src, discord = FakeSource(), FakeDiscord()
    cfg = Config(
        session_user="me",
        watch=[WatchTarget("myalt")],
        schedule=Schedule(quiet_hours=None, heartbeat_hour=25, jitter=0),
    )
    app = App(cfg, Store(":memory:"), discord, lambda: src, now=lambda: clock["now"],
              rng=random.Random(0), sleep=lambda s: None)
    return app, src, discord, clock


def advance(clock, hours):
    clock["now"] += timedelta(hours=hours)


def pings(discord):
    return [m for m in discord.sent if m["ping"]]


def test_first_run_is_baseline_without_pings(env):
    app, src, discord, clock = env
    assert app.tick()
    assert pings(discord) == []
    assert any("Baseline" in m["content"] for m in discord.sent)


def test_new_follower_pings(env):
    app, src, discord, clock = env
    app.tick()
    src.followers["3"] = "carol"
    advance(clock, 3.1)
    app.tick()
    [msg] = pings(discord)
    assert "New follower on @myalt" in msg["embeds"][0]["title"]
    assert "carol" in msg["embeds"][0]["description"]


def test_new_following_pings(env):
    app, src, discord, clock = env
    app.tick()
    src.following["10"] = "newclub"
    advance(clock, 3.1)
    app.tick()
    [msg] = pings(discord)
    assert "started following" in msg["embeds"][0]["title"]


def test_unchanged_counts_skip_full_fetch(env):
    app, src, discord, clock = env
    app.tick()
    src.calls.clear()
    advance(clock, 3.1)
    app.tick()
    assert src.calls == [("profile", "myalt")]


def test_not_due_does_nothing(env):
    app, src, discord, clock = env
    app.tick()
    src.calls.clear()
    advance(clock, 1)
    app.tick()
    assert src.calls == []


def test_unfollow_debounced_across_two_runs(env):
    app, src, discord, clock = env
    app.tick()
    del src.followers["2"]
    advance(clock, 3.1)
    app.tick()
    assert pings(discord) == []
    advance(clock, 3.1)
    app.tick()  # count unchanged but pending-missing forces the second fetch
    [msg] = pings(discord)
    assert "lost 1 follower" in msg["embeds"][0]["title"]


def test_incomplete_fetch_does_not_diff(env):
    app, src, discord, clock = env
    app.tick()
    src.reported_followers = 50  # profile claims far more than the list returned
    advance(clock, 3.1)
    assert not app.tick()
    assert pings(discord) == []


def test_auth_error_pauses_and_alerts_once(env):
    app, src, discord, clock = env
    src.error = AuthError("checkpoint required")
    assert not app.tick()
    advance(clock, 3.1)
    assert not app.tick()
    alerts = [m for m in pings(discord) if "fresh login" in m["content"]]
    assert len(alerts) == 1
    assert app.store.get("auth_broken")


def test_rate_limit_backs_off(env):
    app, src, discord, clock = env
    src.error = RateLimited("429")
    assert not app.tick()
    src.error = None
    src.calls.clear()
    advance(clock, 3.1)
    app.tick()
    assert src.calls == []  # backed off to ~2x interval


def test_failed_discord_send_is_retried(env):
    app, src, discord, clock = env
    app.tick()
    src.followers["3"] = "carol"
    discord.fail = True
    advance(clock, 3.1)
    app.tick()
    discord.fail = False
    advance(clock, 0.25)
    app.tick()
    [msg] = pings(discord)
    assert "carol" in msg["embeds"][0]["description"]


def test_removals_can_be_silenced(env):
    app, src, discord, clock = env
    app.cfg.watch[0].report_removals = False
    app.tick()
    del src.followers["2"]
    for _ in range(2):
        advance(clock, 3.1)
        app.tick()
    assert pings(discord) == []
    assert app.store.recent_changes()[0]["change"] == "removed"


def test_event_posts_baseline_then_notify(env):
    app, src, discord, clock = env
    app.cfg.event_pages = ["venue"]
    src.posts = [PostInfo("p1", "venue", "abc", "old post", T0 - timedelta(days=2), "")]
    app.tick()
    assert any("Now watching @venue" in m["content"] for m in discord.sent)
    src.posts.append(PostInfo("p2", "venue", "def", "Live DJ tonight 9pm, $5 cover 📍 The Loft", T0, "https://img"))
    advance(clock, 3.1)
    app.tick()
    embeds = [e for m in discord.sent for e in m["embeds"]]
    assert len(embeds) == 1
    assert embeds[0]["title"].startswith("📅 Event")
    assert embeds[0]["url"] == "https://www.instagram.com/p/def/"


def test_quiet_hours_skip(env):
    app, src, discord, clock = env
    hour = T0.astimezone().hour
    app.cfg.schedule.quiet_hours = (hour, (hour + 1) % 24)
    app.tick()
    assert src.calls == []
