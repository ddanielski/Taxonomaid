"""Unit tests for the JSONL pending-decision log adapter."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from taxonomaid.adapters.pending_log import JsonlPendingLog
from taxonomaid.domain import FileSystemError, PendingDecision, PendingState

pytestmark = pytest.mark.unit


def _pending(decision_id: str = "abc") -> PendingDecision:
    return PendingDecision(
        decision_id=decision_id,
        ts=datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC),
        unsorted_path=Path("/tmp/_unsorted/foo.pdf"),
        proposed_destination=Path("/tmp/Documents/Foo"),
        destination_root=Path("/tmp"),
        confidence=0.4,
        reason="maybe",
    )


async def test_append_and_get_round_trip(tmp_path: Path) -> None:
    log = JsonlPendingLog(tmp_path / "pending.jsonl")
    pending = _pending()
    await log.append(pending)
    fetched = await log.get(pending.decision_id)
    assert fetched is not None
    assert fetched.unsorted_path == pending.unsorted_path
    assert fetched.state is PendingState.REQUESTED


async def test_transition_records_new_state(tmp_path: Path) -> None:
    log = JsonlPendingLog(tmp_path / "pending.jsonl")
    pending = _pending()
    await log.append(pending)
    await log.transition(pending.decision_id, to=PendingState.APPLIED)

    fetched = await log.get(pending.decision_id)
    assert fetched is not None
    assert fetched.state is PendingState.APPLIED


async def test_transition_unknown_id_raises(tmp_path: Path) -> None:
    log = JsonlPendingLog(tmp_path / "pending.jsonl")
    with pytest.raises(FileSystemError):
        await log.transition("nope", to=PendingState.APPLIED)


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        (PendingState.APPLIED, PendingState.REQUESTED),
        (PendingState.APPLIED, PendingState.ANSWERED),
        (PendingState.ANSWERED, PendingState.REQUESTED),
        (PendingState.REQUESTED, PendingState.REQUESTED),
    ],
)
async def test_invalid_state_transition_is_rejected(
    tmp_path: Path,
    from_state: PendingState,
    to_state: PendingState,
) -> None:
    log = JsonlPendingLog(tmp_path / "pending.jsonl")
    pending = _pending()
    await log.append(pending)
    if from_state is not PendingState.REQUESTED:
        # Walk the legal predecessor chain to land in `from_state`.
        await log.transition(pending.decision_id, to=PendingState.ANSWERED)
        if from_state is PendingState.APPLIED:
            await log.transition(pending.decision_id, to=PendingState.APPLIED)

    with pytest.raises(FileSystemError, match="legal successor"):
        await log.transition(pending.decision_id, to=to_state)


async def test_legacy_record_without_destination_root_is_recovered(tmp_path: Path) -> None:
    """Pre-§1.4 records are missing destination_root; recover structurally."""
    legacy_path = tmp_path / "pending.jsonl"
    legacy_record = {
        "decision_id": "legacy",
        "ts": "2026-05-17T12:00:00+00:00",
        "unsorted_path": "/srv/docs/_unsorted/foo.pdf",
        "proposed_destination": "/srv/docs/Receipts",
        "confidence": 0.4,
        "reason": "old miner output",
        "state": "requested",
    }
    legacy_path.write_text(json.dumps(legacy_record) + "\n", encoding="utf-8")

    log = JsonlPendingLog(legacy_path)
    pending = await log.get("legacy")
    assert pending is not None
    assert pending.destination_root == Path("/srv/docs")


async def test_replay_yields_latest_state_per_id(tmp_path: Path) -> None:
    log = JsonlPendingLog(tmp_path / "pending.jsonl")
    a = _pending("a")
    b = _pending("b")
    await log.append(a)
    await log.append(b)
    await log.transition("a", to=PendingState.ANSWERED)

    states: dict[str, PendingState] = {}
    async for entry in log.replay():
        states[entry.decision_id] = entry.state

    assert states["a"] is PendingState.ANSWERED
    assert states["b"] is PendingState.REQUESTED
