"""Pure follower/following diff logic. No I/O, fully unit-tested."""
from __future__ import annotations

import math
from dataclasses import dataclass

ACTIVE = "active"
GONE = "gone"


@dataclass(frozen=True)
class Rel:
    username: str
    status: str = ACTIVE
    missing: int = 0  # consecutive complete snapshots this user was absent from


@dataclass(frozen=True)
class Change:
    change: str  # "added" | "removed"
    user_id: str
    username: str


def is_complete(fetched: int, reported: int) -> bool:
    """Instagram's reported counts drift a little (deactivated accounts etc.),
    but a fetch far below the count means pagination was cut short."""
    if reported <= 0:
        return True
    tolerance = max(2, math.ceil(reported * 0.02))
    return fetched >= reported - tolerance


def diff(state: dict[str, Rel], current: dict[str, str], debounce: int = 2) -> tuple[dict[str, Rel], list[Change]]:
    """Compare a complete snapshot (user_id -> username) to stored state.

    Additions are reported immediately. A removal is only reported once the user
    has been missing from `debounce` consecutive complete snapshots, because
    Instagram's lists occasionally omit people transiently.
    """
    new_state: dict[str, Rel] = {}
    changes: list[Change] = []

    for uid, username in current.items():
        old = state.get(uid)
        if old is None or old.status == GONE:
            changes.append(Change("added", uid, username))
        new_state[uid] = Rel(username)

    for uid, old in state.items():
        if uid in current:
            continue
        if old.status == GONE:
            new_state[uid] = old
            continue
        missing = old.missing + 1
        if missing >= debounce:
            changes.append(Change("removed", uid, old.username))
            new_state[uid] = Rel(old.username, GONE, 0)
        else:
            new_state[uid] = Rel(old.username, ACTIVE, missing)

    return new_state, changes
