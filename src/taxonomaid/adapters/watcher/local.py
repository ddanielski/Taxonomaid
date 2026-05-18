"""Local filesystem watcher backed by :mod:`watchfiles`.

Each :class:`WatchConfig` is followed by its own ``awatch`` coroutine so
the per-watch ``recursive`` flag is honoured (a single ``awatch`` call
takes one ``recursive`` value for the whole call). Events that fall
inside a watch's ``unsorted_dir`` are dropped so the dispatcher doesn't
react to its own output.

Each pump task is wrapped in a backoff/reconnect loop so transient
filesystem hiccups (NFS reconnect, watched directory briefly removed,
permission error) don't tear the daemon down. This mirrors the
inbound-notifier reconnect policy and keeps the dispatcher running for
the long uptimes a NAS deployment expects.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog
from watchfiles import Change, awatch

from taxonomaid.domain import FileEvent, FileEventKind

if TYPE_CHECKING:
    from collections.abc import Sequence

    from taxonomaid.config import WatchConfig

_log = structlog.get_logger("taxonomaid.watcher")

_INITIAL_BACKOFF_S: Final[float] = 1.0
_MAX_BACKOFF_S: Final[float] = 60.0


_CHANGE_MAP: dict[Change, FileEventKind] = {
    Change.added: FileEventKind.ADDED,
    Change.modified: FileEventKind.MODIFIED,
    Change.deleted: FileEventKind.DELETED,
}


class LocalWatcher:
    """:class:`taxonomaid.ports.Watcher` backed by ``watchfiles.awatch``."""

    def __init__(self, watches: Sequence[WatchConfig]) -> None:
        self._watches = tuple(watches)
        self._stop = asyncio.Event()

    async def watch(self) -> AsyncIterator[FileEvent]:
        """Stream events from every configured watch via a multiplex queue."""
        if not self._watches:
            return

        queue: asyncio.Queue[FileEvent | None] = asyncio.Queue()
        producers = [
            asyncio.create_task(
                self._pump_watch(watch, queue),
                name=f"taxonomaid.watch[{watch.path}]",
            )
            for watch in self._watches
        ]

        try:
            remaining = len(producers)
            while remaining > 0:
                event = await queue.get()
                if event is None:
                    remaining -= 1
                    continue
                yield event
        finally:
            self._stop.set()
            for task in producers:
                task.cancel()
            for task in producers:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    _log.warning(
                        "watch_pump_shutdown_error",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )

    async def stop(self) -> None:
        """Signal the underlying ``awatch`` loops to exit."""
        self._stop.set()

    async def _pump_watch(
        self,
        watch: WatchConfig,
        queue: asyncio.Queue[FileEvent | None],
    ) -> None:
        """Forward events from one watch, restarting on transient failures.

        ``awatch`` typically dies when the watched root is briefly
        unmounted, replaced, or hits a permission error. Rather than
        cancelling the dispatcher's whole TaskGroup we log, sleep with
        capped exponential backoff, and retry - matching the resilience
        of the inbound notifier loop.
        """
        # Resolved once at task start - if an operator plants a
        # symlink-tray at runtime, the cached value lags reality. The
        # filter is still safe: the dispatcher applies
        # :func:`safe_unsorted_dir` on every event, and events that
        # resolve outside the watch root fall through the
        # :func:`_is_under` check below. The only fallout from a stale
        # cache is that ``_unsorted/`` events the operator just
        # symlinked away might not be suppressed *here*; the
        # ``_is_under(resolved, watch_root_resolved)`` guard catches
        # them on the very next line. Re-resolving on every event
        # would burn a ``stat`` per change set on slow filesystems.
        unsorted_root = (watch.destination_root / watch.unsorted_dir).resolve()
        watch_root_resolved = watch.path.resolve()
        backoff_s = _INITIAL_BACKOFF_S
        try:
            while not self._stop.is_set():
                try:
                    async for change_set in awatch(
                        str(watch.path),
                        stop_event=self._stop,
                        recursive=watch.recursive,
                    ):
                        backoff_s = _INITIAL_BACKOFF_S
                        for change, raw_path in change_set:
                            kind = _CHANGE_MAP.get(change)
                            if kind is None:
                                continue
                            path = Path(raw_path)
                            resolved = path.resolve()
                            if _is_under(resolved, unsorted_root):
                                continue
                            if not _is_under(resolved, watch_root_resolved):
                                continue
                            # Always emit the resolved path so the
                            # dispatcher's RecentlyMoved cache and the
                            # path_safety helpers see the same key
                            # whether the watch root is a symlink, an
                            # NFS bind-mount, or a plain directory.
                            await queue.put(
                                FileEvent(
                                    path=resolved,
                                    kind=kind,
                                    watch_root=watch.path,
                                    destination_root=watch.destination_root,
                                    unsorted_dir=watch.unsorted_dir,
                                )
                            )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # `watchfiles` can surface OSError from the kernel
                    # *or* RuntimeError / ValueError from its Rust
                    # backend on weird mount transitions (NFS reconnect,
                    # bind-mount swap, FUSE hiccup). Catching the wider
                    # ``Exception`` here mirrors the inbound notifier
                    # loop's resilience.
                    _log.warning(
                        "watch_pump_reconnecting",
                        watch=str(watch.path),
                        error=str(exc),
                        error_type=type(exc).__name__,
                        backoff_s=backoff_s,
                    )
                    try:
                        await asyncio.sleep(backoff_s)
                    except asyncio.CancelledError:
                        raise
                    backoff_s = min(backoff_s * 2, _MAX_BACKOFF_S)
                    continue
                # `awatch` returned without raising -> stop_event was set.
                return
        finally:
            await queue.put(None)


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True
