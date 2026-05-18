"""Pending-decision log port."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from taxonomaid.domain import PendingDecision, PendingState


@runtime_checkable
class PendingLog(Protocol):
    """Append-only log of pending decisions plus state-transition helpers.

    Implementations must persist atomically; later updates must not
    rewrite earlier entries (we use append-only state transitions so a
    crashed process is recoverable from the file alone).
    """

    async def append(self, pending: PendingDecision) -> None:
        """Register a new pending decision in state ``REQUESTED``."""
        ...

    async def transition(self, decision_id: str, *, to: PendingState) -> None:
        """Append a state-transition record for ``decision_id``."""
        ...

    async def get(self, decision_id: str) -> PendingDecision | None:
        """Return the latest version of ``decision_id``, or ``None``."""
        ...

    def replay(self) -> AsyncIterator[PendingDecision]:
        """Yield the latest version of every pending decision."""
        ...
