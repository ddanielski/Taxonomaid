"""Decision records and decision provenance."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any


class DecisionSource(StrEnum):
    """Where a decision came from.

    Used for analytics, miner weighting, and audit trails.
    """

    RULE = "rule"
    LLM = "llm"
    NOTIFIER_CONFIRMED = "notifier_confirmed"
    USER_OVERRIDE = "user_override"


@dataclass(frozen=True, slots=True)
class Decision:
    """An immutable record of a single placement decision.

    Attributes:
        decision_id: Random hex correlator (26 chars) shared with the
            outbound notification. Not time-sortable; use :attr:`ts` for
            ordering.
        ts: UTC timestamp at which the decision was finalised.
        file: Source path of the file at the moment of decision.
        destination: Target directory the file was placed in.
        source: Which subsystem produced the decision.
        confidence: ``[0.0, 1.0]`` confidence assigned to the decision.
        rule_id: ID of the rule that fired, if ``source == RULE``.
        reason: Human-readable explanation, primarily for LLM decisions.
        features: Free-form structured metadata used by the miner.
    """

    decision_id: str
    ts: datetime
    file: Path
    destination: Path
    source: DecisionSource
    confidence: float
    rule_id: str | None = None
    reason: str | None = None
    features: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate invariants that the type system can't express.

        Also wraps ``features`` in a :class:`MappingProxyType` so the
        outer dict is read-only -
        ``decision.features['leak'] = ...`` raises ``TypeError``. This
        is **shallow** immutability: a caller who stores a mutable
        value (e.g. ``features={"tokens": [...]}``) can still mutate
        the inner list. The dispatcher only puts immutable scalars
        (``bool``, ``float``, ``str``) into ``features`` today, so
        this hasn't bitten anything in practice; the type stays
        ``Mapping[str, Any]`` to keep the schema flexible for the
        miner's training payloads.
        """
        if not 0.0 <= self.confidence <= 1.0:
            msg = f"confidence must be in [0, 1]; got {self.confidence!r}"
            raise ValueError(msg)
        if self.ts.tzinfo is None:
            msg = "Decision.ts must be timezone-aware (use UTC)"
            raise ValueError(msg)
        # Reject non-UTC zones: the JSONL serialiser emits the
        # ``ts.isoformat()`` verbatim, so a Berlin-zoned timestamp
        # would land in the audit log as ``...+02:00``. The miner and
        # auditor assume every timestamp is comparable on the same
        # offset (UTC); a mixed-tz log would silently mis-order
        # decisions across daylight-saving transitions. Aware-zero is
        # the contract; ``datetime.now(UTC)`` and
        # ``SystemClock.now()`` both satisfy it.
        if self.ts.utcoffset() != timedelta(0):
            msg = f"Decision.ts must be UTC (offset 0); got offset {self.ts.utcoffset()!r}"
            raise ValueError(msg)
        if self.source is DecisionSource.RULE and self.rule_id is None:
            msg = "rule_id is required when source=RULE"
            raise ValueError(msg)
        if not isinstance(self.features, MappingProxyType):
            object.__setattr__(self, "features", MappingProxyType(dict(self.features)))
