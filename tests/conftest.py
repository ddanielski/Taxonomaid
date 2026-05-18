"""Shared pytest fixtures and fakes for unit + integration tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from taxonomaid.domain import Decision, DecisionSource, FileEvent, FileSystemError
from taxonomaid.ports import LLMResponse, NotifierResponse


class FakeClock:
    """Deterministic clock for unit tests."""

    def __init__(self, *, fixed: datetime | None = None) -> None:
        self._fixed = fixed or datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._fixed

    def set(self, value: datetime) -> None:
        self._fixed = value


class FakeFilesystem:
    """In-memory filesystem fake covering the :class:`FilesystemPort` surface."""

    def __init__(self) -> None:
        self._files: dict[Path, bytes] = {}
        self._dirs: set[Path] = set()
        self.moves: list[tuple[Path, Path]] = []

    def exists(self, path: Path) -> bool:
        return path in self._files or path in self._dirs

    def is_file(self, path: Path) -> bool:
        return path in self._files

    def mkdir(self, path: Path, *, parents: bool = True, exist_ok: bool = True) -> None:
        if path in self._dirs and not exist_ok:
            msg = f"FakeFilesystem: {path} exists"
            raise FileExistsError(msg)
        if parents:
            for parent in path.parents:
                self._dirs.add(parent)
        self._dirs.add(path)

    def move(self, src: Path, dst: Path) -> None:
        if src not in self._files:
            msg = f"FakeFilesystem: {src} not present"
            raise FileNotFoundError(msg)
        if dst in self._files or dst in self._dirs:
            # Mirror LocalFilesystem.move (which raises the port-level
            # FileSystemError) so dispatcher tests see the same
            # exception surface as production.
            msg = f"FakeFilesystem: refusing to overwrite {dst}"
            raise FileSystemError(msg)
        self.mkdir(dst.parent, parents=True, exist_ok=True)
        self._files[dst] = self._files.pop(src)
        self.moves.append((src, dst))

    def size(self, path: Path) -> int:
        return len(self._files[path])

    def write(self, path: Path, data: bytes) -> None:
        """Test-only helper: place a file in the fake filesystem."""
        self.mkdir(path.parent, parents=True, exist_ok=True)
        self._files[path] = data


class FakeWatcher:
    """Watcher that streams a scripted sequence of events.

    The fake closes the stream once the script is exhausted, which
    naturally terminates the dispatcher's watch loop in tests.
    """

    def __init__(self, events: list[FileEvent]) -> None:
        self._events = list(events)
        self._stop = asyncio.Event()

    async def watch(self) -> AsyncIterator[FileEvent]:
        for event in self._events:
            if self._stop.is_set():
                return
            yield event
            await asyncio.sleep(0)

    async def stop(self) -> None:
        self._stop.set()


class RecordedLLM:
    """LLM provider that returns scripted responses in order."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    async def classify(
        self,
        *,
        filename: str,
        excerpt: str,
        candidate_destinations: tuple[Path, ...],
        prior_user_moves: tuple[tuple[str, Path], ...] = (),
    ) -> LLMResponse:
        del candidate_destinations, prior_user_moves
        self.calls.append((filename, excerpt[:64]))
        if not self._responses:
            msg = "RecordedLLM script exhausted"
            raise AssertionError(msg)
        return self._responses.pop(0)


class FakeNotifierOutbound:
    """Outbound notifier that records its calls instead of sending.

    Records per-file pending prompts in :attr:`sent`, rule-review
    proposals in :attr:`rule_proposals`, and review summaries in
    :attr:`review_completes` / :attr:`review_nudges`. Each list is
    append-only so tests can check ordering.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.rule_proposals: list[dict[str, object]] = []
        self.review_completes: list[dict[str, object]] = []
        self.review_nudges: list[dict[str, object]] = []
        self.audit_findings: list[dict[str, object]] = []
        self.circuit_opens: list[dict[str, object]] = []
        self.circuit_recovers: list[dict[str, object]] = []

    async def notify_pending(
        self,
        *,
        decision_id: str,
        file: Path,
        proposed_destination: Path,
        confidence: float,
        reason: str,
    ) -> None:
        self.sent.append(
            {
                "decision_id": decision_id,
                "file": file,
                "proposed_destination": proposed_destination,
                "confidence": confidence,
                "reason": reason,
            }
        )

    async def notify_rule_proposal(
        self,
        *,
        proposal: object,
        sample_filenames: tuple[str, ...] = (),
        index: int | None = None,
        total: int | None = None,
    ) -> None:
        self.rule_proposals.append(
            {
                "proposal": proposal,
                "sample_filenames": sample_filenames,
                "index": index,
                "total": total,
            }
        )

    async def notify_review_complete(self, *, approved: int, rejected: int) -> None:
        self.review_completes.append({"approved": approved, "rejected": rejected})

    async def notify_review_nudge(self, *, pending: int) -> None:
        self.review_nudges.append({"pending": pending})

    async def notify_audit_findings(self, *, findings_by_kind: dict[str, tuple[str, ...]]) -> None:
        self.audit_findings.append({"findings_by_kind": findings_by_kind})

    async def notify_circuit_open(self, *, reason: str) -> None:
        self.circuit_opens.append({"reason": reason})

    async def notify_circuit_recovered(self, *, skipped_files: int) -> None:
        self.circuit_recovers.append({"skipped_files": skipped_files})


class FakeNotifierInbound:
    """Inbound notifier that yields a scripted list of replies."""

    def __init__(self, responses: list[NotifierResponse]) -> None:
        self._responses = list(responses)
        self._stop = asyncio.Event()

    async def stream(self) -> AsyncIterator[NotifierResponse]:
        for response in self._responses:
            if self._stop.is_set():
                return
            yield response
            await asyncio.sleep(0)

    async def stop(self) -> None:
        self._stop.set()


@pytest.fixture()
def fake_clock() -> FakeClock:
    """Deterministic clock fixed to 2026-05-17 12:00 UTC."""
    return FakeClock()


@pytest.fixture()
def fake_fs() -> FakeFilesystem:
    """Fresh in-memory filesystem fake per test."""
    return FakeFilesystem()


@pytest.fixture()
def sample_decision(fake_clock: FakeClock) -> Decision:
    """A minimal valid :class:`Decision` for serialisation tests."""
    return Decision(
        decision_id="01HV2P3Q4R5S6T7U8V9W0XYZ12",
        ts=fake_clock.now(),
        file=Path("inbox/foo.pdf"),
        destination=Path("Documents/Foo/"),
        source=DecisionSource.LLM,
        confidence=0.91,
        reason="filename matches Foo pattern",
        features={"tokens": ["foo", "pdf"]},
    )
