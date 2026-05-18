"""Decision log port: append-only sink for every placement decision."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from taxonomaid.domain import Decision


@runtime_checkable
class DecisionLog(Protocol):
    """Append-only audit log of placement decisions.

    Implementations must persist atomically; a partial write is worse than
    a missing write because the miner consumes this file.
    """

    async def append(self, decision: Decision) -> None:
        """Append ``decision`` to the log."""
        ...

    def replay(self) -> AsyncIterator[Decision]:
        """Yield every decision in insertion order, oldest first."""
        ...
