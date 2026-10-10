"""Job orchestration: decides what is due, runs it, and turns results into Discord messages."""
from __future__ import annotations

import json
import logging
import random
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Callable

from .config import Config, WatchTarget
from .diff import Change, Rel, diff, is_complete
from .extract import EventInfo, extract
from .notify import Discord, post_embed, relation_embeds
from .sources.base import AuthError, PostInfo, RateLimited, Source, SourceError
from .store import Store, parse_iso

log = logging.getLogger(__name__)

AUTH_BROKEN = "auth_broken"
FAILURE_ALERT_AT = 3


class App:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        notifier: Discord,
        source_factory: Callable[[], Source],
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        rng: random.Random | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.cfg = cfg
        self.store = store
        self.notifier = notifier
        self.source_factory = source_factory
        self.now = now
        self.rng = rng or random.Random()
        self.sleep = sleep
        self._source: Source | None = None

    def source(self) -> Source:
        if self._source is None:
            self._source = self.source_factory()
        return self._source

    # --- scheduling ---
    def jobs(self) -> dict[str, tuple[float, Callable[[datetime], None]]]:
        s = self.cfg.schedule
        jobs: dict[str, tuple[float, Callable[[datetime], None]]] = {
            f"relations:{t.handle}": (s.relations_hours, lambda now, t=t: self.run_relations(t, now))
            for t in self.cfg.watch
        }
        if self.cfg.event_pages:
            jobs["posts"] = (s.posts_hours, self.run_posts)
        return jobs

    def next_run(self, now: datetime, hours: float) -> datetime:
        j = self.cfg.schedule.jitter
        return now + timedelta(hours=hours * (1 + self.rng.uniform(-j, j)))

    def in_quiet_hours(self, now: datetime) -> bool:
        quiet = self.cfg.schedule.quiet_hours
        if not quiet:
            return False
        start, end = quiet
        hour = now.astimezone().hour
        return start <= hour < end if start <= end else hour >= start or hour < end

    def tick(self, force: list[str] | None = None) -> bool:
        """Run whatever is due. `force` runs the named jobs (or "all") right now,
        ignoring schedule and quiet hours. Returns False if something needs attention."""
        now = self.now()
        self.flush()
        self.maybe_heartbeat(now)

        if self.store.get(AUTH_BROKEN):
            log.warning("Instagram session marked broken; skipping until `sitk login`")
            return False
        if force is None and self.in_quiet_hours(now):
            log.info("quiet hours, skipping Instagram jobs")
            return True

        ok = True
        for name, (hours, fn) in self.jobs().items():
            if force is None:
                job = self.store.get_job(name)
                due_at = parse_iso(job["next_run_at"]) if job else None
                if due_at and due_at > now:
                    continue
            elif "all" not in force and name not in force:
                continue

            log.info("running %s", name)
            try:
                fn(now)
                self.store.job_ok(name, self.next_run(now, hours), now)
            except AuthError as e:
                self.mark_auth_broken(str(e))
                return False
            except RateLimited as e:
                log.warning("rate limited during %s: %s", name, e)
                self.record_failure(name, self.next_run(now, hours * 2), f"rate limited: {e}")
                return False  # stop all Instagram traffic this tick
            except Exception as e:  # noqa: BLE001 - one bad job shouldn't kill the others
                log.exception("job %s failed", name)
                self.record_failure(name, self.next_run(now, hours), repr(e))
                ok = False
        self.flush()
        return ok

    def record_failure(self, name: str, next_run: datetime, error: str) -> None:
        failures = self.store.job_failed(name, next_run, error)
        if failures == FAILURE_ALERT_AT:
            self.safe_send(f"⚠️ `{name}` has failed {failures} times in a row: {error[:300]}", ping=True)

    def mark_auth_broken(self, message: str) -> None:
        if self.store.get(AUTH_BROKEN):
            return
        self.store.set(AUTH_BROKEN, message[:500] or "session invalid")
        log.error("Instagram session broken: %s", message)
        self.safe_send(
            "⚠️ Instagram rejected the session (logged out, challenged, or flagged as automated), "
            "so polling is **paused**.\n"
            "Open Instagram in your browser and clear any prompt it shows, then run "
            "`sitk login --browser <browser>` to resume.\n"
            f"Reason: {message[:300]}",
            ping=True,
        )

    def safe_send(self, content: str = "", embeds: list[dict] | None = None, ping: bool = False) -> bool:
        try:
            self.notifier.send(content, embeds, ping=ping)
            return True
        except Exception:  # noqa: BLE001
            log.exception("Discord send failed")
            return False

    # --- heartbeat ---
    def maybe_heartbeat(self, now: datetime) -> None:
        local = now.astimezone()
        today = local.date().isoformat()
        if local.hour < self.cfg.schedule.heartbeat_hour or self.store.get("heartbeat_date") == today:
            return
        lines = ["💓 Still running."]
        if broken := self.store.get(AUTH_BROKEN):
            lines.append(f"⚠️ Instagram session broken: {broken[:200]}")
        for job in self.store.all_jobs():
            last = parse_iso(job["last_ok_at"])
            when = last.astimezone().strftime("%a %-I:%M %p") if last else "never"
            err = f" (failing: {job['failures']}×)" if job["failures"] else ""
            lines.append(f"`{job['name']}` last OK {when}{err}")
        lines.append(f"Changes in the last 24h: {self.store.changes_since(now - timedelta(days=1))}")
        if self.safe_send("\n".join(lines)):
            self.store.set("heartbeat_date", today)

    # --- followers / following ---
    def run_relations(self, target: WatchTarget, now: datetime) -> None:
        src = self.source()
        info = src.profile(target.handle)
        if not info.viewer_can_see:
            raise SourceError(f"@{target.handle} is private and the session account doesn't follow it")

        incomplete = []
        for kind in target.kinds():
            reported = info.followers if kind == "followers" else info.following
            snap = self.store.get_snapshot(target.handle, kind)
            full_due = snap is None or now - parse_iso(snap["taken_at"]) >= timedelta(
                hours=self.cfg.schedule.full_refresh_hours
            )
            if (snap is not None and snap["reported"] == reported and not full_due
                    and not self.store.has_pending_missing(target.handle, kind)):
                log.info("@%s %s count unchanged (%d), skipping full fetch", target.handle, kind, reported)
                continue

            current = src.relations(target.handle, kind)
            if not is_complete(len(current), reported):
                log.warning("@%s %s fetch incomplete: got %d of %d", target.handle, kind, len(current), reported)
                incomplete.append(f"{kind} {len(current)}/{reported}")
                continue

            state = self.store.load_relations(target.handle, kind)
            if not state:
                self.store.save_relations(target.handle, kind, {u: Rel(n) for u, n in current.items()}, now)
                self.store.set_snapshot(target.handle, kind, reported, len(current), now)
                self.safe_send(f"📌 Baseline saved: @{target.handle} {kind} ({len(current)} accounts). "
                               "You'll be pinged about changes from here on.")
                continue

            new_state, changes = diff(state, current)
            self.store.save_relations(target.handle, kind, new_state, now)
            self.store.set_snapshot(target.handle, kind, reported, len(current), now)
            if changes:
                log.info("@%s %s: %d change(s)", target.handle, kind, len(changes))
                self.store.record_changes(target.handle, kind, changes, now, target.report_removals)

        if incomplete:
            raise SourceError(f"incomplete list fetch for @{target.handle}: {', '.join(incomplete)}")

    # --- event pages ---
    def run_posts(self, now: datetime) -> None:
        src = self.source()
        for i, handle in enumerate(self.cfg.event_pages):
            if i:
                self.sleep(self.rng.uniform(5, 20))  # don't fire requests back to back
            posts = src.recent_posts(handle)
            if not posts:
                continue
            account = posts[0].account
            baseline = not self.store.has_posts(account)
            for post in sorted(posts, key=lambda p: p.posted_at):
                if self.store.post_seen(post.post_id):
                    continue
                info = extract(post.caption, post.posted_at)
                self.store.add_post(post, info.is_event, info.as_dict(), notified=baseline, now=now)
            if baseline:
                self.safe_send(f"📌 Now watching @{account} for new posts ({len(posts)} existing posts recorded).")

    # --- delivery (retries anything not yet sent) ---
    def flush(self) -> None:
        groups: dict[tuple[str, str], list] = defaultdict(list)
        for row in self.store.unnotified_changes():
            groups[(row["account"], row["kind"])].append(row)
        for (account, kind), rows in groups.items():
            changes = [Change(r["change"], r["user_id"], r["username"]) for r in rows]
            if self.safe_send(embeds=relation_embeds(account, kind, changes), ping=True):
                self.store.mark_changes_notified([r["id"] for r in rows])

        for row in self.store.unnotified_posts():
            post = PostInfo(row["post_id"], row["account"], row["shortcode"], row["caption"] or "",
                            parse_iso(row["posted_at"]), row["media_url"] or "")
            info = EventInfo(**json.loads(row["parsed_json"]))
            if self.safe_send(embeds=[post_embed(post, info)]):
                self.store.mark_notified(row["post_id"])
