"""Filesystem watcher port.

Abstracted so the dispatcher can be unit-tested against scripted events
without depending on inotify or :mod:`watchfiles`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from taxonomaid.domain import FileEvent


@runtime_checkable
class Watcher(Protocol):
    """Source of filesystem events for the dispatcher.

    The contract is **single-shot**: ``watch()`` may be called at most
    once per instance. Once :meth:`stop` (or the consumer breaking out
    of the iterator) winds the loop down, calling ``watch()`` again
    must return immediately without yielding. Construct a fresh
    instance to re-watch.
    """

    def watch(self) -> AsyncIterator[FileEvent]:
        """Stream events from every configured watched root.

        Single-shot: the iterator must not be re-used after the loop
        terminates or :meth:`stop` is called.
        """
        ...

    async def stop(self) -> None:
        """Tear down the watch loop. Idempotent."""
        ...
