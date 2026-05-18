"""Feedback machinery: recently-moved cache and user-override detection.

The dispatcher uses :class:`RecentlyMoved` to:

1. Suppress its own follow-up ``ADDED`` events when a placement triggers
   the watcher seeing the new file under ``destination_root``.
2. Identify user overrides: a ``DELETED`` or ``MODIFIED`` event on a path
   it just placed indicates the user moved it elsewhere.

The cache is in-memory; restarts forget. That is acceptable for Phase 4
because the value is short-term (24h TTL).
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


@dataclass(frozen=True, slots=True)
class _Entry:
    decision_id: str
    placed_at: datetime


class RecentlyMoved:
    """Bounded TTL cache of files the dispatcher recently placed.

    Args:
        ttl: How long an entry survives before being treated as stale.
        max_entries: Hard cap on the cache to bound memory.
    """

    def __init__(
        self,
        *,
        ttl: timedelta = timedelta(hours=24),
        max_entries: int = 1000,
    ) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._entries: OrderedDict[Path, _Entry] = OrderedDict()

    def remember(self, *, path: Path, decision_id: str, placed_at: datetime) -> None:
        """Record that ``path`` was just placed by us."""
        self._entries[path] = _Entry(decision_id=decision_id, placed_at=placed_at)
        self._entries.move_to_end(path)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def matches_recent_placement(self, path: Path, *, now: datetime) -> bool:
        """Return ``True`` when ``path`` is in the cache and not stale."""
        return self._lookup(path, now=now) is not None

    def consume_override(self, path: Path, *, now: datetime) -> str | None:
        """Drop the entry for ``path`` and return its ``decision_id``.

        Used when a user override is detected, so subsequent events on
        the same path don't keep re-firing the same negative signal.
        """
        entry = self._lookup(path, now=now)
        if entry is None:
            return None
        self._entries.pop(path, None)
        return entry.decision_id

    def _lookup(self, path: Path, *, now: datetime) -> _Entry | None:
        entry = self._entries.get(path)
        if entry is None:
            return None
        if now - entry.placed_at > self._ttl:
            self._entries.pop(path, None)
            return None
        return entry
