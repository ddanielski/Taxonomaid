"""Unit tests for the dry-run wrapper adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from taxonomaid.domain import (
    Decision,
    DecisionSource,
    PendingDecision,
    PendingState,
)
from taxonomaid.services.dry_run_wrappers import (
    DryRunDecisionLog,
    DryRunFilesystem,
    DryRunPendingLog,
)

pytestmark = pytest.mark.unit


# ---- Filesystem wrapper -----------------------------------------------------


class _RecordingFilesystem:
    """Inner filesystem that records every call for assertion."""

    def __init__(self, *, exists_paths: set[Path] | None = None) -> None:
        self._exists = exists_paths or set()
        self.move_calls: list[tuple[Path, Path]] = []
        self.mkdir_calls: list[Path] = []

    def exists(self, path: Path) -> bool:
        return path in self._exists

    def is_file(self, path: Path) -> bool:
        return path in self._exists

    def size(self, path: Path) -> int:
        del path
        return 0

    def mkdir(self, path: Path, *, parents: bool = True, exist_ok: bool = True) -> None:
        del parents, exist_ok
        self.mkdir_calls.append(path)

    def move(self, src: Path, dst: Path) -> None:
        self.move_calls.append((src, dst))


def test_filesystem_forwards_reads(tmp_path: Path) -> None:
    """``exists / is_file / size`` delegate to the inner filesystem."""
    target = tmp_path / "f.pdf"
    inner = _RecordingFilesystem(exists_paths={target})
    fs = DryRunFilesystem(inner)
    assert fs.exists(target) is True
    assert fs.is_file(target) is True
    assert fs.size(target) == 0


def test_filesystem_discards_move(tmp_path: Path) -> None:
    """``move`` does NOT touch the inner filesystem."""
    inner = _RecordingFilesystem()
    fs = DryRunFilesystem(inner)
    fs.move(tmp_path / "src", tmp_path / "dst")
    assert inner.move_calls == []  # critical: nothing happened


def test_filesystem_discards_mkdir(tmp_path: Path) -> None:
    """``mkdir`` does NOT touch the inner filesystem."""
    inner = _RecordingFilesystem()
    fs = DryRunFilesystem(inner)
    fs.mkdir(tmp_path / "new")
    assert inner.mkdir_calls == []


# ---- DecisionLog wrapper ----------------------------------------------------


class _RecordingDecisionLog:
    """Inner decision log that records appends and yields nothing on replay."""

    def __init__(self) -> None:
        self.appends: list[Decision] = []

    async def append(self, decision: Decision) -> None:
        self.appends.append(decision)

    async def replay(self) -> AsyncIterator[Decision]:
        if False:  # pragma: no cover - generator stub
            yield


def _decision() -> Decision:
    return Decision(
        decision_id="abc",
        ts=datetime.now(tz=UTC),
        file=Path("/x/foo.pdf"),
        destination=Path("Reports"),
        source=DecisionSource.LLM,
        confidence=0.9,
    )


async def test_decision_log_discards_appends() -> None:
    """``append`` does NOT touch the inner log."""
    inner = _RecordingDecisionLog()
    log = DryRunDecisionLog(inner)
    await log.append(_decision())
    assert inner.appends == []


async def test_decision_log_forwards_replay() -> None:
    """``replay`` returns the inner log's iterator unchanged."""
    inner = _RecordingDecisionLog()
    log = DryRunDecisionLog(inner)
    items = [d async for d in log.replay()]
    assert items == []  # inner yields nothing; wrapper preserves that


# ---- PendingLog wrapper -----------------------------------------------------


class _RecordingPendingLog:
    """Inner pending log that records writes and stays read-empty."""

    def __init__(self) -> None:
        self.appends: list[PendingDecision] = []
        self.transitions: list[tuple[str, PendingState]] = []
        self.gets: list[str] = []

    async def append(self, pending: PendingDecision) -> None:
        self.appends.append(pending)

    async def transition(self, decision_id: str, *, to: PendingState) -> None:
        self.transitions.append((decision_id, to))

    async def get(self, decision_id: str) -> PendingDecision | None:
        self.gets.append(decision_id)
        return None

    async def replay(self) -> AsyncIterator[PendingDecision]:
        if False:  # pragma: no cover - generator stub
            yield


def _pending() -> PendingDecision:
    return PendingDecision(
        decision_id="abc",
        ts=datetime.now(tz=UTC),
        unsorted_path=Path("/x/_unsorted/foo.pdf"),
        proposed_destination=Path("Reports"),
        destination_root=Path("/x"),
        confidence=0.5,
        reason="test",
        state=PendingState.REQUESTED,
    )


async def test_pending_log_discards_append_and_transition() -> None:
    """``append`` and ``transition`` do NOT touch the inner log."""
    inner = _RecordingPendingLog()
    log = DryRunPendingLog(inner)
    await log.append(_pending())
    await log.transition("abc", to=PendingState.APPLIED)
    assert inner.appends == []
    assert inner.transitions == []


async def test_pending_log_forwards_get() -> None:
    """``get`` delegates to the inner log so orphan recovery sees truth."""
    inner = _RecordingPendingLog()
    log = DryRunPendingLog(inner)
    result = await log.get("abc")
    assert result is None
    assert inner.gets == ["abc"]
