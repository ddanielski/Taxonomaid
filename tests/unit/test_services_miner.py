"""Unit tests for the pattern miner."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from taxonomaid.domain import (
    CoherenceSpec,
    Decision,
    DecisionSource,
    MatchSpec,
    Rule,
    RuleSource,
)
from taxonomaid.services import Miner

pytestmark = pytest.mark.unit


def _make_decision(
    *,
    file_name: str,
    destination: str,
    source: DecisionSource = DecisionSource.LLM,
    confidence: float = 0.9,
) -> Decision:
    return Decision(
        decision_id="x" + file_name,
        ts=datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC),
        file=Path(file_name),
        destination=Path(destination),
        source=source,
        confidence=confidence,
    )


async def _stream(decisions: list[Decision]) -> AsyncIterator[Decision]:
    for d in decisions:
        yield d


async def test_miner_returns_no_proposal_below_min_samples() -> None:
    miner = Miner(min_samples=10)
    decisions = [
        _make_decision(file_name="invoice_1.pdf", destination="Finance/Invoices") for _ in range(5)
    ]
    proposals = await miner.mine(decisions=_stream(decisions))
    assert proposals == ()


async def test_miner_promotes_stable_token_pattern() -> None:
    miner = Miner(min_samples=5, agreement=0.9)
    decisions = [
        _make_decision(file_name=f"invoice_{i}.pdf", destination="Finance/Invoices")
        for i in range(8)
    ]
    proposals = await miner.mine(decisions=_stream(decisions))
    assert len(proposals) == 1
    proposal = proposals[0]
    assert "invoice" in proposal.rule.id
    assert proposal.rule.match.ext == (".pdf",)
    assert proposal.rule.confidence >= 0.9
    assert proposal.rule.weight == pytest.approx(0.7)
    assert proposal.rule.source is RuleSource.USER_INFERRED


async def test_miner_flags_auto_promotable_above_threshold() -> None:
    miner = Miner(
        min_samples=5,
        agreement=0.9,
        auto_promote_threshold=0.95,
        auto_promote_min_samples=10,
    )
    decisions = [
        _make_decision(file_name=f"receipt_{i}.pdf", destination="Receipts") for i in range(15)
    ]
    proposals = await miner.mine(decisions=_stream(decisions))
    assert len(proposals) == 1
    # auto_promotable is informational only; the rule itself is still
    # tagged USER_INFERRED so it routes through `taxonomaid review`.
    assert proposals[0].auto_promotable is True
    assert proposals[0].rule.source is RuleSource.USER_INFERRED


async def test_miner_skips_rule_decisions_so_existing_rules_are_not_double_counted() -> None:
    miner = Miner(min_samples=5)
    decisions = [
        Decision(
            decision_id=f"d{i}",
            ts=datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC),
            file=Path(f"x_{i}.pdf"),
            destination=Path("Foo"),
            source=DecisionSource.RULE,
            confidence=0.9,
            rule_id="some_rule",
        )
        for i in range(20)
    ]
    proposals = await miner.mine(decisions=_stream(decisions))
    assert proposals == ()


async def test_miner_skips_destinations_under_anchored_prefix() -> None:
    miner = Miner(min_samples=5, agreement=0.9)
    decisions = [
        _make_decision(file_name=f"tax_{i}.pdf", destination="Finance/Taxes/2025")
        for i in range(10)
    ]
    anchored_rule = Rule(
        id="tax_pdf_to_year",
        match=MatchSpec(filename_regex=r"(?i)tax", ext=(".pdf",)),
        destination_template="Finance/Taxes/{year}/",
        coherence=CoherenceSpec(year_match=True),
        anchored=True,
    )
    proposals = await miner.mine(decisions=_stream(decisions), existing_rules=(anchored_rule,))
    assert proposals == ()


async def test_miner_low_precision_blocks_proposal() -> None:
    miner = Miner(min_samples=5, agreement=0.9)
    decisions: list[Decision] = []
    decisions.extend(
        _make_decision(file_name=f"shared_{i}.pdf", destination="DestA") for i in range(5)
    )
    decisions.extend(
        _make_decision(file_name=f"shared_{i}.pdf", destination="DestB") for i in range(5)
    )
    proposals = await miner.mine(decisions=_stream(decisions))
    assert proposals == ()


async def test_miner_uses_dominant_extension() -> None:
    miner = Miner(min_samples=5, agreement=0.8)
    decisions = [
        _make_decision(file_name=f"report_{i}.docx", destination="Reports") for i in range(8)
    ]
    proposals = await miner.mine(decisions=_stream(decisions))
    assert len(proposals) == 1
    assert proposals[0].rule.match.ext == (".docx",)


async def test_miner_skips_anchored_prefix_when_destination_is_absolute() -> None:
    """C2 regression: anchored prefix protects absolute decision destinations.

    Decisions land in the log with absolute destinations
    (``/srv/.../Finance/Taxes/2025``) but anchored rule templates
    yield relative prefixes (``Finance/Taxes``). The earlier
    ``startswith`` check never matched - the "anchored rules are
    never modified" guarantee was effectively a no-op. The
    subsequence-on-parts check makes both coordinate systems agree.
    """
    miner = Miner(min_samples=5, agreement=0.8)
    anchored_rule = Rule(
        id="anchored_taxes",
        match=MatchSpec(),
        destination_template="Finance/Taxes/{year}/",
        weight=1.0,
        confidence=0.95,
        anchored=True,
    )
    decisions = [
        _make_decision(
            file_name=f"tax_{i}.pdf",
            destination=f"/srv/share/Documents/Finance/Taxes/{2020 + i}",
        )
        for i in range(10)
    ]
    proposals = await miner.mine(
        decisions=_stream(decisions),
        existing_rules=(anchored_rule,),
    )
    assert proposals == ()


async def test_miner_emits_ext_none_when_no_extension_dominates() -> None:
    """Mixed extensions below the agreement threshold yield ``ext=None``.

    The resulting rule fires across all extensions; the precision
    recheck on historical samples is the safety net (a rule that
    matches noise won't pass the agreement bar). This is the
    documented trade-off in :mod:`taxonomaid.services.miner` and a
    user who wants stricter matching should hand-edit
    ``proposed_rules.yaml`` before approving via ``taxonomaid review``.
    """
    miner = Miner(min_samples=5, agreement=0.8)
    decisions = [
        _make_decision(file_name="receipt_1.pdf", destination="Finance/Receipts"),
        _make_decision(file_name="receipt_2.png", destination="Finance/Receipts"),
        _make_decision(file_name="receipt_3.jpg", destination="Finance/Receipts"),
        _make_decision(file_name="receipt_4.heic", destination="Finance/Receipts"),
        _make_decision(file_name="receipt_5.pdf", destination="Finance/Receipts"),
        _make_decision(file_name="receipt_6.png", destination="Finance/Receipts"),
    ]
    proposals = await miner.mine(decisions=_stream(decisions))
    assert len(proposals) == 1
    assert proposals[0].rule.match.ext is None
