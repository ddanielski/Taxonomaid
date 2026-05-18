"""Pending-decision domain type.

When the LLM's confidence is below ``auto_move`` the file is parked in
``_unsorted/`` and a :class:`PendingDecision` is appended to
``data/pending_decisions.jsonl``. The dispatcher reconciles user replies
from the notifier against this log.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class PendingState(StrEnum):
    """Lifecycle of a pending decision."""

    REQUESTED = "requested"
    ANSWERED = "answered"
    APPLIED = "applied"


@dataclass(frozen=True, slots=True)
class PendingDecision:
    """A file parked in ``_unsorted/`` awaiting user action.

    Attributes:
        decision_id: Random hex correlator (26 chars) shared with the
            outbound message; not time-sortable, use :attr:`ts` for
            ordering.
        ts: When the pending decision was registered.
        unsorted_path: Where the file currently lives in ``_unsorted/``.
        proposed_destination: The LLM's preferred destination.
        destination_root: The watch's destination root, used to anchor
            user-supplied PROPOSE replies so multi-watch setups can't
            misfile across watches.
        confidence: The LLM's self-reported confidence.
        reason: The LLM's free-text justification (recorded for audit).
        state: Lifecycle state.
    """

    decision_id: str
    ts: datetime
    unsorted_path: Path
    proposed_destination: Path
    destination_root: Path
    confidence: float
    reason: str
    state: PendingState = PendingState.REQUESTED
