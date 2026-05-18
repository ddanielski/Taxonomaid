"""Unit tests for :mod:`taxonomaid.services.feedback`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from taxonomaid.services.feedback import RecentlyMoved

pytestmark = pytest.mark.unit


def _t(seconds: int) -> datetime:
    return datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=seconds)


def test_remember_then_match() -> None:
    cache = RecentlyMoved()
    path = Path("/data/Documents/foo.pdf")
    cache.remember(path=path, decision_id="d1", placed_at=_t(0))
    assert cache.matches_recent_placement(path, now=_t(60))


def test_consume_override_returns_decision_id_once() -> None:
    cache = RecentlyMoved()
    path = Path("/data/Documents/foo.pdf")
    cache.remember(path=path, decision_id="d1", placed_at=_t(0))
    assert cache.consume_override(path, now=_t(60)) == "d1"
    assert cache.consume_override(path, now=_t(60)) is None


def test_entries_expire_after_ttl() -> None:
    cache = RecentlyMoved(ttl=timedelta(seconds=10))
    path = Path("/data/Documents/foo.pdf")
    cache.remember(path=path, decision_id="d1", placed_at=_t(0))
    assert not cache.matches_recent_placement(path, now=_t(60))


def test_eviction_when_over_capacity() -> None:
    cache = RecentlyMoved(max_entries=2)
    p1 = Path("/a")
    p2 = Path("/b")
    p3 = Path("/c")
    cache.remember(path=p1, decision_id="d1", placed_at=_t(0))
    cache.remember(path=p2, decision_id="d2", placed_at=_t(1))
    cache.remember(path=p3, decision_id="d3", placed_at=_t(2))
    assert not cache.matches_recent_placement(p1, now=_t(3))
    assert cache.matches_recent_placement(p2, now=_t(3))
    assert cache.matches_recent_placement(p3, now=_t(3))
