"""Notifier ports.

The notifier is split into two halves so the swap-friendly outbound channel
is decoupled from the per-channel reply protocol.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, runtime_checkable


class NotifierResponseKind(StrEnum):
    """Possible user actions arriving over the inbound channel.

    The first three are per-file decision responses, correlated with
    a pending decision via :attr:`NotifierResponse.decision_id`. The
    last three drive the rule-review session: ``REVIEW_START`` is the
    operator typing ``/review`` in the chat (no id), and the
    ``RULE_*`` actions carry a proposal id (the rule's ``.id`` field)
    in :attr:`NotifierResponse.decision_id`.

    The id field is reused for both namespaces to keep the wire-format
    simple; consumers dispatch on ``kind`` to know which namespace
    applies.
    """

    APPROVE = "approve"
    REJECT = "reject"
    PROPOSE = "propose"
    REVIEW_START = "review_start"
    RULE_APPROVE = "rule_approve"
    RULE_REJECT = "rule_reject"


@dataclass(frozen=True, slots=True)
class NotifierResponse:
    """A single user reply to a pending decision.

    Attributes:
        decision_id: Correlates the reply back to the pending decision.
        kind: Which action the user took.
        proposed_destination: Set only when ``kind == PROPOSE``.
        raw_text: Original message text, for audit and free-form parsing.
        extra: Adapter-specific metadata (e.g. Telegram message id).
    """

    decision_id: str
    kind: NotifierResponseKind
    proposed_destination: Path | None = None
    raw_text: str | None = None
    extra: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Wrap ``extra`` so the frozen guarantee covers dict contents."""
        if not isinstance(self.extra, MappingProxyType):
            object.__setattr__(self, "extra", MappingProxyType(dict(self.extra)))


@runtime_checkable
class NotifierOutbound(Protocol):
    """Send-only side of the notifier."""

    async def notify_pending(
        self,
        *,
        decision_id: str,
        file: Path,
        proposed_destination: Path,
        confidence: float,
        reason: str,
    ) -> None:
        """Ask the user to approve or amend a pending decision.

        Args:
            decision_id: 26-char hex correlator emitted in inline
                keyboard callback data so replies can be attributed.
                Not time-sortable; use the decision log's ``ts`` field
                for ordering.
            file: The file awaiting placement.
            proposed_destination: The LLM's proposed destination.
            confidence: The LLM's self-reported confidence.
            reason: The LLM's free-text justification.

        Raises:
            taxonomaid.domain.NotifierError: On any send failure.
        """
        ...


@runtime_checkable
class NotifierInbound(Protocol):
    """Receive-only side of the notifier.

    Implementations stream :class:`NotifierResponse` events forever; the
    dispatcher consumes them via ``async for`` and reconciles each event
    against ``data/pending_decisions.jsonl``.
    """

    def stream(self) -> AsyncIterator[NotifierResponse]:
        """Yield responses as they arrive."""
        ...

    async def stop(self) -> None:
        """Tear down any background polling loop."""
        ...
