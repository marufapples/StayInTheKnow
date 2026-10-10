from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .diff import ACTIVE, Rel

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS relations (
    account TEXT NOT NULL, kind TEXT NOT NULL, user_id TEXT NOT NULL,
    username TEXT NOT NULL, status TEXT NOT NULL, missing INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL, last_seen TEXT,
    PRIMARY KEY (account, kind, user_id)
);
CREATE TABLE IF NOT EXISTS snapshots (
    account TEXT NOT NULL, kind TEXT NOT NULL,
    reported INTEGER NOT NULL, fetched INTEGER NOT NULL, taken_at TEXT NOT NULL,
    PRIMARY KEY (account, kind)
);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL, kind TEXT NOT NULL, change TEXT NOT NULL,
    user_id TEXT NOT NULL, username TEXT NOT NULL, at TEXT NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS posts (
    post_id TEXT PRIMARY KEY, account TEXT NOT NULL, shortcode TEXT NOT NULL,
    caption TEXT, media_url TEXT, posted_at TEXT, first_seen TEXT NOT NULL,
    is_event INTEGER NOT NULL DEFAULT 0, parsed_json TEXT, notified INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
    name TEXT PRIMARY KEY, next_run_at TEXT, last_ok_at TEXT,
    last_error TEXT, failures INTEGER NOT NULL DEFAULT 0
);
"""


def now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Store:
    def __init__(self, path: Path | str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # --- kv ---
    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str | None) -> None:
        with self.db:
            if value is None:
                self.db.execute("DELETE FROM kv WHERE key = ?", (key,))
            else:
                self.db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))

    # --- relations ---
    def load_relations(self, account: str, kind: str) -> dict[str, Rel]:
        rows = self.db.execute(
            "SELECT user_id, username, status, missing FROM relations WHERE account = ? AND kind = ?",
            (account, kind),
        )
        return {r["user_id"]: Rel(r["username"], r["status"], r["missing"]) for r in rows}

    def save_relations(self, account: str, kind: str, state: dict[str, Rel], now: datetime) -> None:
        ts = now_iso(now)
        with self.db:
            for uid, rel in state.items():
                seen = ts if rel.status == ACTIVE and rel.missing == 0 else None
                self.db.execute(
                    """INSERT INTO relations (account, kind, user_id, username, status, missing, first_seen, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT (account, kind, user_id) DO UPDATE SET
                         username = excluded.username, status = excluded.status, missing = excluded.missing,
                         last_seen = COALESCE(excluded.last_seen, relations.last_seen)""",
                    (account, kind, uid, rel.username, rel.status, rel.missing, ts, seen),
                )

    def has_pending_missing(self, account: str, kind: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM relations WHERE account = ? AND kind = ? AND status = ? AND missing > 0 LIMIT 1",
            (account, kind, ACTIVE),
        ).fetchone()
        return row is not None

    def record_changes(self, account: str, kind: str, changes, now: datetime, notify_removals: bool = True) -> None:
        with self.db:
            self.db.executemany(
                "INSERT INTO changes (account, kind, change, user_id, username, at, notified) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(account, kind, c.change, c.user_id, c.username, now_iso(now),
                  int(c.change == "removed" and not notify_removals)) for c in changes],
            )

    def unnotified_changes(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM changes WHERE notified = 0 ORDER BY id").fetchall()

    def mark_changes_notified(self, ids: list[int]) -> None:
        with self.db:
            self.db.executemany("UPDATE changes SET notified = 1 WHERE id = ?", [(i,) for i in ids])

    def recent_changes(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM changes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def changes_since(self, since: datetime) -> int:
        return self.db.execute("SELECT COUNT(*) FROM changes WHERE at >= ?", (now_iso(since),)).fetchone()[0]

    # --- snapshots (metadata of the last complete fetch) ---
    def get_snapshot(self, account: str, kind: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM snapshots WHERE account = ? AND kind = ?", (account, kind)
        ).fetchone()

    def set_snapshot(self, account: str, kind: str, reported: int, fetched: int, now: datetime) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO snapshots (account, kind, reported, fetched, taken_at) VALUES (?, ?, ?, ?, ?)",
                (account, kind, reported, fetched, now_iso(now)),
            )

    # --- posts ---
    def post_seen(self, post_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM posts WHERE post_id = ?", (post_id,)).fetchone() is not None

    def has_posts(self, account: str) -> bool:
        return self.db.execute("SELECT 1 FROM posts WHERE account = ? LIMIT 1", (account,)).fetchone() is not None

    def add_post(self, post, is_event: bool, parsed: dict, notified: bool, now: datetime) -> None:
        with self.db:
            self.db.execute(
                """INSERT OR IGNORE INTO posts
                   (post_id, account, shortcode, caption, media_url, posted_at, first_seen, is_event, parsed_json, notified)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (post.post_id, post.account, post.shortcode, post.caption, post.media_url,
                 now_iso(post.posted_at), now_iso(now), int(is_event), json.dumps(parsed), int(notified)),
            )

    def unnotified_posts(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM posts WHERE notified = 0 ORDER BY posted_at").fetchall()

    def mark_notified(self, post_id: str) -> None:
        with self.db:
            self.db.execute("UPDATE posts SET notified = 1 WHERE post_id = ?", (post_id,))

    # --- jobs ---
    def get_job(self, name: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM jobs WHERE name = ?", (name,)).fetchone()

    def all_jobs(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM jobs ORDER BY name").fetchall()

    def job_ok(self, name: str, next_run: datetime, now: datetime) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO jobs (name, next_run_at, last_ok_at, last_error, failures) VALUES (?, ?, ?, NULL, 0)
                   ON CONFLICT (name) DO UPDATE SET next_run_at = excluded.next_run_at,
                     last_ok_at = excluded.last_ok_at, last_error = NULL, failures = 0""",
                (name, now_iso(next_run), now_iso(now)),
            )

    def job_failed(self, name: str, next_run: datetime, error: str) -> int:
        with self.db:
            self.db.execute(
                """INSERT INTO jobs (name, next_run_at, last_error, failures) VALUES (?, ?, ?, 1)
                   ON CONFLICT (name) DO UPDATE SET next_run_at = excluded.next_run_at,
                     last_error = excluded.last_error, failures = jobs.failures + 1""",
                (name, now_iso(next_run), error[:500]),
            )
        return self.get_job(name)["failures"]
