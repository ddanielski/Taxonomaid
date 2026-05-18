"""Unit tests for the JSONL decision-log adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from taxonomaid.adapters.decision_log import JsonlDecisionLog
from taxonomaid.domain import Decision

pytestmark = pytest.mark.unit


async def _collect(it: object) -> list[Decision]:
    out: list[Decision] = []
    async for item in it:  # type: ignore[attr-defined]
        out.append(item)
    return out


async def test_append_then_replay_round_trip(tmp_path: Path, sample_decision: Decision) -> None:
    log = JsonlDecisionLog(tmp_path / "decisions.jsonl")
    await log.append(sample_decision)

    replayed = await _collect(log.replay())
    assert len(replayed) == 1
    got = replayed[0]
    assert got.decision_id == sample_decision.decision_id
    assert got.destination == sample_decision.destination
    assert got.confidence == pytest.approx(sample_decision.confidence)


async def test_replay_empty_when_file_missing(tmp_path: Path) -> None:
    log = JsonlDecisionLog(tmp_path / "missing.jsonl")
    replayed = await _collect(log.replay())
    assert replayed == []


async def test_append_creates_parent_dir(tmp_path: Path, sample_decision: Decision) -> None:
    target = tmp_path / "deeply" / "nested" / "log.jsonl"
    log = JsonlDecisionLog(target)
    await log.append(sample_decision)
    assert target.exists()
