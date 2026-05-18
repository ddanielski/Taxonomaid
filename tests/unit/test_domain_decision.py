"""Unit tests for :mod:`taxonomaid.domain.decision`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from taxonomaid.domain import Decision, DecisionSource

pytestmark = pytest.mark.unit


def test_decision_minimal_construction() -> None:
    decision = Decision(
        decision_id="abc",
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        file=Path("foo.pdf"),
        destination=Path("Docs/"),
        source=DecisionSource.LLM,
        confidence=0.5,
    )
    assert decision.confidence == 0.5
    assert decision.features == {}


def test_decision_rejects_naive_timestamp() -> None:
    naive = datetime.fromisoformat("2026-01-01T00:00:00")
    with pytest.raises(ValueError, match="timezone-aware"):
        Decision(
            decision_id="abc",
            ts=naive,
            file=Path("foo.pdf"),
            destination=Path("Docs/"),
            source=DecisionSource.LLM,
            confidence=0.5,
        )


def test_decision_rejects_non_utc_timezone() -> None:
    """Sev-3 regression: a non-UTC zone-aware timestamp is refused.

    The JSONL serialiser persists ``ts.isoformat()`` verbatim, so a
    Berlin-zoned timestamp would land as ``...+02:00`` in the audit
    log. The miner and auditor compare timestamps directly; mixing
    offsets silently mis-orders decisions across daylight-saving
    boundaries. We require UTC (offset 0).
    """
    berlin = timezone(timedelta(hours=2))
    with pytest.raises(ValueError, match="UTC"):
        Decision(
            decision_id="abc",
            ts=datetime(2026, 6, 1, 12, 0, tzinfo=berlin),
            file=Path("foo.pdf"),
            destination=Path("Docs/"),
            source=DecisionSource.LLM,
            confidence=0.5,
        )


@pytest.mark.parametrize("bad", [-0.01, 1.01, 5.0])
def test_decision_rejects_out_of_range_confidence(bad: float) -> None:
    with pytest.raises(ValueError, match="confidence"):
        Decision(
            decision_id="abc",
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            file=Path("foo.pdf"),
            destination=Path("Docs/"),
            source=DecisionSource.LLM,
            confidence=bad,
        )


def test_decision_features_outer_mapping_is_immutable() -> None:
    decision = Decision(
        decision_id="abc",
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        file=Path("foo.pdf"),
        destination=Path("Docs/"),
        source=DecisionSource.LLM,
        confidence=0.5,
        features={"a": 1},
    )
    with pytest.raises(TypeError):
        decision.features["b"] = 2  # type: ignore[index]


def test_decision_rule_source_requires_rule_id() -> None:
    with pytest.raises(ValueError, match="rule_id"):
        Decision(
            decision_id="abc",
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            file=Path("foo.pdf"),
            destination=Path("Docs/"),
            source=DecisionSource.RULE,
            confidence=1.0,
        )
