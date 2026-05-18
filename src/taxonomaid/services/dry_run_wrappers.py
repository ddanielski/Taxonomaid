"""Read-through, write-discard adapter wrappers for ``--dry-run`` mode.

Why this exists
---------------

``taxonomaid bootstrap --dry-run`` previews what classification +
placement decisions would be made on real data without actually
committing to them. The LLM and rule engine still run for real -
that's the whole point - so the operator can see the LLM's outputs
on their actual file corpus before letting the daemon move
anything.

Only the **write** side of each I/O port is suppressed:

- ``DryRunFilesystem.move`` and ``mkdir`` log a "would..." event
  and discard. ``exists / is_file / size`` forward to the inner
  filesystem so the dispatcher's existence checks (e.g. "is this
  destination already in the candidate set?") see truth.
- ``DryRunDecisionLog.append`` is discarded. ``replay`` forwards,
  so the similarity-index warmup uses real prior decisions and
  the dry run's LLM context matches what production would see.
- ``DryRunPendingLog.append / transition`` are discarded.
  ``replay / get`` forward, so orphan-pending recovery (which
  only does state transitions in dry-run, also discarded) sees
  the real durable state.

The wrappers implement the same ``Protocol`` shapes as the real
adapters; the dispatcher receives them via ``DispatcherDeps`` and
treats them as opaque ports. No dispatcher code path knows about
dry-run mode - which is the property that makes this safe.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import structlog

from taxonomaid.domain import Decision, PendingDecision, PendingState
from taxonomaid.ports import DecisionLog, FilesystemPort, PendingLog

_log = structlog.get_logger(__name__)


class DryRunFilesystem:
    """Filesystem wrapper that logs writes and forwards reads.

    Args:
        inner: The real filesystem to delegate reads to.
    """

    def __init__(self, inner: FilesystemPort) -> None:
        self._inner = inner

    def exists(self, path: Path) -> bool:
        """Forward to the real filesystem."""
        return self._inner.exists(path)

    def is_file(self, path: Path) -> bool:
        """Forward to the real filesystem."""
        return self._inner.is_file(path)

    def size(self, path: Path) -> int:
        """Forward to the real filesystem."""
        return self._inner.size(path)

    def mkdir(self, path: Path, *, parents: bool = True, exist_ok: bool = True) -> None:
        """Log the would-create event and discard.

        Args:
            path: Directory the dispatcher wants to create.
            parents: Ignored - dry-run never creates anything.
            exist_ok: Ignored - dry-run never creates anything.
        """
        del parents, exist_ok
        if not self._inner.exists(path):
            _log.info("dry_run_would_mkdir", path=str(path))

    def move(self, src: Path, dst: Path) -> None:
        """Log the would-move event and discard."""
        _log.info("dry_run_would_move", src=str(src), dst=str(dst))


class DryRunDecisionLog:
    """Decision log wrapper that discards appends and forwards replays.

    Reads forward to the real log so similarity-index warm-up sees
    the real prior history and the LLM context matches production.

    Args:
        inner: The real decision log to delegate reads to.
    """

    def __init__(self, inner: DecisionLog) -> None:
        self._inner = inner

    async def append(self, decision: Decision) -> None:
        """Log the would-append event and discard."""
        _log.info(
            "dry_run_would_log_decision",
            decision_id=decision.decision_id,
            source=decision.source.value,
            destination=str(decision.destination),
            confidence=decision.confidence,
        )

    def replay(self) -> AsyncIterator[Decision]:
        """Forward to the real log."""
        return self._inner.replay()


class DryRunPendingLog:
    """Pending-log wrapper that discards writes and forwards reads.

    Reads forward to the real log so orphan-pending recovery and
    the inbound responder see real durable state. Writes (append +
    transition) are logged-and-discarded so a dry run produces no
    new pending entries the operator hasn't agreed to.

    Args:
        inner: The real pending log to delegate reads to.
    """

    def __init__(self, inner: PendingLog) -> None:
        self._inner = inner

    async def append(self, pending: PendingDecision) -> None:
        """Log the would-park event and discard."""
        _log.info(
            "dry_run_would_park",
            decision_id=pending.decision_id,
            unsorted_path=str(pending.unsorted_path),
            proposed=str(pending.proposed_destination),
            confidence=pending.confidence,
        )

    async def transition(self, decision_id: str, *, to: PendingState) -> None:
        """Log the would-transition event and discard."""
        _log.debug(
            "dry_run_would_transition",
            decision_id=decision_id,
            to=to.value,
        )

    async def get(self, decision_id: str) -> PendingDecision | None:
        """Forward to the real log."""
        return await self._inner.get(decision_id)

    def replay(self) -> AsyncIterator[PendingDecision]:
        """Forward to the real log."""
        return self._inner.replay()
