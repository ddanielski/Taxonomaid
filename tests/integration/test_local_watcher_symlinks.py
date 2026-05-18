"""Integration tests for symlink behaviour in :class:`LocalWatcher`.

The N5 fix made the watcher emit *resolved* paths into ``FileEvent``
so the dispatcher's :class:`RecentlyMoved` cache and the path-safety
helpers see a single canonical key whether the watch root is a symlink
or a real directory. These tests pin that behaviour by monkeypatching
:func:`watchfiles.awatch` with a controlled change-set, so the test
runs deterministically without relying on inotify timing.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from watchfiles import Change

from taxonomaid.adapters.watcher.local import LocalWatcher
from taxonomaid.config import WatchConfig
from taxonomaid.domain import FileEvent

pytestmark = pytest.mark.integration


def _fake_awatch(change_set: set[tuple[Change, str]]) -> Any:
    """Build an ``awatch``-compatible async generator that yields one batch."""

    async def _runner(
        *_args: object,
        stop_event: asyncio.Event | None = None,
        recursive: bool = True,
        **_kwargs: object,
    ) -> AsyncIterator[set[tuple[Change, str]]]:
        del recursive
        yield change_set
        if stop_event is not None:
            await stop_event.wait()

    return _runner


async def test_local_watcher_emits_resolved_paths_through_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link_dir = tmp_path / "link"
    link_dir.symlink_to(real_dir)

    # Drop a file the watcher will see "added".
    sym_path = link_dir / "foo.txt"
    sym_path.write_text("hello", encoding="utf-8")
    real_path = (real_dir / "foo.txt").resolve()

    monkeypatch.setattr(
        "taxonomaid.adapters.watcher.local.awatch",
        _fake_awatch({(Change.added, str(sym_path))}),
    )

    watch = WatchConfig(path=link_dir, destination_root=link_dir)
    watcher = LocalWatcher([watch])

    events: list[FileEvent] = []
    async for event in watcher.watch():
        events.append(event)
        await watcher.stop()
        break

    assert len(events) == 1
    # Critical assertion: the emitted path is the resolved real path,
    # not the symlinked one we started with.
    assert events[0].path == real_path
    assert events[0].path != sym_path


async def test_local_watcher_drops_events_inside_unsorted_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The under-``_unsorted/`` filter actually drops self-caused events.

    Both a real and a parked event arrive in the same batch; only the
    real one should make it through. This covers the filter line in
    :class:`LocalWatcher._pump_watch` - the previous version of this
    test passed even with the filter deleted, because it stopped the
    watcher before the producer ever ran.
    """
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    unsorted = watch_root / "_unsorted"
    unsorted.mkdir()

    real = watch_root / "real.txt"
    real.write_text("real", encoding="utf-8")
    parked = unsorted / "parked.txt"
    parked.write_text("parked", encoding="utf-8")

    monkeypatch.setattr(
        "taxonomaid.adapters.watcher.local.awatch",
        _fake_awatch({(Change.added, str(real)), (Change.added, str(parked))}),
    )

    watcher = LocalWatcher([WatchConfig(path=watch_root, destination_root=watch_root)])
    events: list[FileEvent] = []
    async for event in watcher.watch():
        events.append(event)
        # The fake change_set is single-shot; one event in is enough.
        await watcher.stop()
        break

    assert [e.path for e in events] == [real.resolve()]
