"""Edge-case dispatcher tests: errors, idempotency, and degenerate replies."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from taxonomaid.adapters.clock import SystemClock
from taxonomaid.adapters.decision_log import JsonlDecisionLog
from taxonomaid.adapters.filesystem import LocalFilesystem
from taxonomaid.adapters.pending_log import JsonlPendingLog
from taxonomaid.config import (
    AppConfig,
    LLMConfig,
    NotifierConfig,
    Thresholds,
    WatchConfig,
    WatchesConfig,
    write_rules_file,
)
from taxonomaid.domain import (
    DecisionSource,
    FileEvent,
    FileEventKind,
    FileSystemError,
    LLMError,
    MatchSpec,
    NotifierError,
    PendingState,
    Rule,
    RuleSource,
)
from taxonomaid.ports import LLMResponse, NotifierResponse, NotifierResponseKind
from taxonomaid.services import ReviewPaths, ReviewSession
from taxonomaid.services.circuit_breaker import CircuitState, LLMCircuit
from taxonomaid.services.dispatcher import Dispatcher, DispatcherDeps
from taxonomaid.services.rule_engine import RuleEngine
from tests.conftest import (
    FakeNotifierInbound,
    FakeNotifierOutbound,
    FakeWatcher,
    RecordedLLM,
)

pytestmark = pytest.mark.integration


def _config(watch_root: Path, *, data_dir: Path) -> AppConfig:
    return AppConfig(
        watches=WatchesConfig(
            watches=(
                WatchConfig(
                    path=watch_root,
                    destination_root=watch_root,
                ),
            ),
        ),
        llm=LLMConfig(
            api_key="x",
            thresholds=Thresholds(
                auto_move=0.75,
                auto_create_folder=0.85,
                auto_promote_rule=0.97,
            ),
        ),
        notifier=NotifierConfig(),
        data_dir=data_dir,
    )


def _event(path: Path, root: Path) -> FileEvent:
    return FileEvent(
        path=path,
        kind=FileEventKind.ADDED,
        watch_root=root,
        destination_root=root,
        unsorted_dir=Path("_unsorted"),
    )


def _drop(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")


def _make_deps(
    *,
    config: AppConfig,
    events: list[FileEvent],
    llm: RecordedLLM,
    outbound: FakeNotifierOutbound | None = None,
    inbound: FakeNotifierInbound | None = None,
    pending_log: JsonlPendingLog | None = None,
    decision_log: JsonlDecisionLog | None = None,
    review_session: object | None = None,
    llm_circuit: object | None = None,
) -> DispatcherDeps:
    return DispatcherDeps(
        config=config,
        rule_engine=RuleEngine(rules=()),
        llm=llm,
        notifier_outbound=outbound,
        notifier_inbound=inbound,
        filesystem=LocalFilesystem(),
        decision_log=decision_log or JsonlDecisionLog(config.data_dir / "decisions.jsonl"),
        pending_log=pending_log or JsonlPendingLog(config.data_dir / "pending_decisions.jsonl"),
        watcher=FakeWatcher(events),
        clock=SystemClock(),
        debounce_s=0.0,
        review_session=review_session,  # type: ignore[arg-type]
        llm_circuit=llm_circuit,  # type: ignore[arg-type]
    )


async def test_modified_events_are_ignored(tmp_path: Path) -> None:
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    src = watch_root / "f.txt"
    _drop(src)

    config = _config(watch_root, data_dir=tmp_path / "data")
    event = FileEvent(
        path=src,
        kind=FileEventKind.MODIFIED,
        watch_root=watch_root,
        destination_root=watch_root,
        unsorted_dir=Path("_unsorted"),
    )
    llm = RecordedLLM([])
    deps = _make_deps(config=config, events=[event], llm=llm)
    await Dispatcher(deps).run()
    assert llm.calls == []


async def test_llm_error_parks_file_with_reason(tmp_path: Path) -> None:
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    src = watch_root / "broken.bin"
    _drop(src)

    class FailingLLM:
        async def classify(
            self,
            *,
            filename: str,
            excerpt: str,
            candidate_destinations: tuple[Path, ...],
            prior_user_moves: tuple[tuple[str, Path], ...] = (),
        ) -> LLMResponse:
            del filename, excerpt, candidate_destinations, prior_user_moves
            msg = "rate limited"
            raise LLMError(msg)

    config = _config(watch_root, data_dir=tmp_path / "data")
    outbound = FakeNotifierOutbound()
    deps = DispatcherDeps(
        config=config,
        rule_engine=RuleEngine(rules=()),
        llm=FailingLLM(),
        notifier_outbound=outbound,
        notifier_inbound=None,
        filesystem=LocalFilesystem(),
        decision_log=JsonlDecisionLog(config.data_dir / "decisions.jsonl"),
        pending_log=JsonlPendingLog(config.data_dir / "pending_decisions.jsonl"),
        watcher=FakeWatcher([_event(src, watch_root)]),
        clock=SystemClock(),
        debounce_s=0.0,
    )
    await Dispatcher(deps).run()

    parked = watch_root / "_unsorted" / "broken.bin"
    assert parked.exists()
    assert len(outbound.sent) == 1
    assert "LLM error" in str(outbound.sent[0]["reason"])


async def test_medium_confidence_with_missing_folder_falls_back_to_unsorted(
    tmp_path: Path,
) -> None:
    """0.75 <= confidence < auto_create_folder: move only if folder exists."""
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    src = watch_root / "f.txt"
    _drop(src)

    config = _config(watch_root, data_dir=tmp_path / "data")
    llm = RecordedLLM(
        [
            LLMResponse(
                destination=Path("DoesNotExist"),
                confidence=0.78,
                reason="confident in classification, folder missing",
            ),
        ]
    )
    outbound = FakeNotifierOutbound()
    deps = _make_deps(
        config=config,
        events=[_event(src, watch_root)],
        llm=llm,
        outbound=outbound,
    )
    await Dispatcher(deps).run()

    parked = watch_root / "_unsorted" / "f.txt"
    assert parked.exists()
    assert len(outbound.sent) == 1


async def test_very_high_confidence_creates_missing_folder(tmp_path: Path) -> None:
    """confidence >= auto_create_folder: dispatcher creates the folder and moves."""
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    src = watch_root / "f.txt"
    _drop(src)

    config = _config(watch_root, data_dir=tmp_path / "data")
    llm = RecordedLLM(
        [
            LLMResponse(
                destination=Path("BrandNew"),
                confidence=0.95,
                reason="document type unambiguous",
            ),
        ]
    )
    outbound = FakeNotifierOutbound()
    deps = _make_deps(
        config=config,
        events=[_event(src, watch_root)],
        llm=llm,
        outbound=outbound,
    )
    await Dispatcher(deps).run()

    moved = watch_root / "BrandNew" / "f.txt"
    assert moved.exists()
    assert outbound.sent == []


async def test_unknown_decision_id_is_warned_not_errored(tmp_path: Path) -> None:
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    config = _config(watch_root, data_dir=tmp_path / "data")
    inbound = FakeNotifierInbound(
        [
            NotifierResponse(
                decision_id="ghost",
                kind=NotifierResponseKind.APPROVE,
                raw_text="/approve ghost",
            )
        ]
    )
    deps = _make_deps(
        config=config,
        events=[],
        llm=RecordedLLM([]),
        inbound=inbound,
    )
    await Dispatcher(deps).run()


async def test_propose_without_path_is_a_warning(tmp_path: Path) -> None:
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    src = watch_root / "f.txt"
    _drop(src)

    config = _config(watch_root, data_dir=tmp_path / "data")
    pending_log = JsonlPendingLog(config.data_dir / "pending_decisions.jsonl")
    decision_log = JsonlDecisionLog(config.data_dir / "decisions.jsonl")
    outbound = FakeNotifierOutbound()
    llm = RecordedLLM([LLMResponse(destination=Path("X"), confidence=0.4, reason="low")])
    deps = _make_deps(
        config=config,
        events=[_event(src, watch_root)],
        llm=llm,
        outbound=outbound,
        pending_log=pending_log,
        decision_log=decision_log,
    )
    await Dispatcher(deps).run()
    decision_id = str(outbound.sent[0]["decision_id"])

    inbound = FakeNotifierInbound(
        [
            NotifierResponse(
                decision_id=decision_id,
                kind=NotifierResponseKind.PROPOSE,
                proposed_destination=None,
                raw_text=f"/move {decision_id}",
            )
        ]
    )
    deps2 = _make_deps(
        config=config,
        events=[],
        llm=RecordedLLM([]),
        outbound=outbound,
        inbound=inbound,
        pending_log=pending_log,
        decision_log=decision_log,
    )
    await Dispatcher(deps2).run()

    parked = watch_root / "_unsorted" / "f.txt"
    assert parked.exists()
    pending = await pending_log.get(decision_id)
    assert pending is not None
    assert pending.state is PendingState.REQUESTED


async def test_already_applied_is_idempotent(tmp_path: Path) -> None:
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    src = watch_root / "f.txt"
    _drop(src)

    config = _config(watch_root, data_dir=tmp_path / "data")
    pending_log = JsonlPendingLog(config.data_dir / "pending_decisions.jsonl")
    decision_log = JsonlDecisionLog(config.data_dir / "decisions.jsonl")
    outbound = FakeNotifierOutbound()
    llm = RecordedLLM([LLMResponse(destination=Path("Foo"), confidence=0.4, reason="low")])
    deps = _make_deps(
        config=config,
        events=[_event(src, watch_root)],
        llm=llm,
        outbound=outbound,
        pending_log=pending_log,
        decision_log=decision_log,
    )
    await Dispatcher(deps).run()
    decision_id = str(outbound.sent[0]["decision_id"])

    first_inbound = FakeNotifierInbound(
        [
            NotifierResponse(
                decision_id=decision_id,
                kind=NotifierResponseKind.APPROVE,
                raw_text=f"/approve {decision_id}",
            )
        ]
    )
    deps2 = _make_deps(
        config=config,
        events=[],
        llm=RecordedLLM([]),
        outbound=outbound,
        inbound=first_inbound,
        pending_log=pending_log,
        decision_log=decision_log,
    )
    await Dispatcher(deps2).run()

    second_inbound = FakeNotifierInbound(
        [
            NotifierResponse(
                decision_id=decision_id,
                kind=NotifierResponseKind.APPROVE,
                raw_text=f"/approve {decision_id}",
            )
        ]
    )
    deps3 = _make_deps(
        config=config,
        events=[],
        llm=RecordedLLM([]),
        outbound=outbound,
        inbound=second_inbound,
        pending_log=pending_log,
        decision_log=decision_log,
    )
    await Dispatcher(deps3).run()

    sources: list[DecisionSource] = []
    async for entry in decision_log.replay():
        sources.append(entry.source)
    assert sources.count(DecisionSource.NOTIFIER_CONFIRMED) == 1


async def test_outbound_failure_does_not_break_dispatch(tmp_path: Path) -> None:
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    src = watch_root / "f.txt"
    _drop(src)

    class BoomOutbound:
        async def notify_pending(
            self,
            *,
            decision_id: str,
            file: Path,
            proposed_destination: Path,
            confidence: float,
            reason: str,
        ) -> None:
            del decision_id, file, proposed_destination, confidence, reason
            msg = "telegram down"
            raise NotifierError(msg)

    config = _config(watch_root, data_dir=tmp_path / "data")
    deps = DispatcherDeps(
        config=config,
        rule_engine=RuleEngine(rules=()),
        llm=RecordedLLM([LLMResponse(destination=Path("Foo"), confidence=0.4, reason="low")]),
        notifier_outbound=BoomOutbound(),
        notifier_inbound=None,
        filesystem=LocalFilesystem(),
        decision_log=JsonlDecisionLog(config.data_dir / "decisions.jsonl"),
        pending_log=JsonlPendingLog(config.data_dir / "pending_decisions.jsonl"),
        watcher=FakeWatcher([_event(src, watch_root)]),
        clock=SystemClock(),
        debounce_s=0.0,
    )
    await Dispatcher(deps).run()

    parked = watch_root / "_unsorted" / "f.txt"
    assert parked.exists()


async def test_llm_escape_parks_with_stay_parked_proposal(tmp_path: Path) -> None:
    """When the LLM proposes an escape path, the notifier shows 'stay parked'.

    The proposed_destination falls back to ``unsorted_path.parent`` so an
    APPROVE tap is a no-op rather than a silent move into the watch root.
    """
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    src = watch_root / "f.txt"
    _drop(src)

    config = _config(watch_root, data_dir=tmp_path / "data")
    pending_log = JsonlPendingLog(config.data_dir / "pending_decisions.jsonl")
    decision_log = JsonlDecisionLog(config.data_dir / "decisions.jsonl")
    outbound = FakeNotifierOutbound()
    llm = RecordedLLM(
        [
            LLMResponse(
                destination=Path("../../etc"),
                confidence=0.5,
                reason="hostile",
            )
        ]
    )
    deps = _make_deps(
        config=config,
        events=[_event(src, watch_root)],
        llm=llm,
        outbound=outbound,
        pending_log=pending_log,
        decision_log=decision_log,
    )
    await Dispatcher(deps).run()

    parked = watch_root / "_unsorted" / "f.txt"
    assert parked.exists()
    assert len(outbound.sent) == 1
    proposed = outbound.sent[0]["proposed_destination"]
    assert proposed == parked.parent
    reason = str(outbound.sent[0]["reason"])
    assert "outside the watch root" in reason
    assert "keep parked" in reason

    decision_id = str(outbound.sent[0]["decision_id"])
    approve_inbound = FakeNotifierInbound(
        [
            NotifierResponse(
                decision_id=decision_id,
                kind=NotifierResponseKind.APPROVE,
                raw_text=f"/approve {decision_id}",
            )
        ]
    )
    deps2 = _make_deps(
        config=config,
        events=[],
        llm=RecordedLLM([]),
        outbound=outbound,
        inbound=approve_inbound,
        pending_log=pending_log,
        decision_log=decision_log,
    )
    await Dispatcher(deps2).run()

    # File is still parked (no rename, no spurious foo (2).txt).
    assert parked.exists()
    assert not (watch_root / "_unsorted" / "f (2).txt").exists()
    pending = await pending_log.get(decision_id)
    assert pending is not None
    assert pending.state is PendingState.ANSWERED


async def test_disappeared_file_is_skipped(tmp_path: Path) -> None:
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    src = watch_root / "transient.txt"

    config = _config(watch_root, data_dir=tmp_path / "data")
    llm = RecordedLLM([])
    deps = _make_deps(
        config=config,
        events=[_event(src, watch_root)],
        llm=llm,
    )
    await Dispatcher(deps).run()
    assert llm.calls == []


async def test_unsorted_dir_symlink_escape_refuses_to_park(tmp_path: Path) -> None:
    """C1 regression: a symlinked ``_unsorted/`` tray pointing outside the watch
    root is treated as an escape attempt - the dispatcher leaves the file in
    place rather than following the symlink.
    """
    if not hasattr(Path, "symlink_to"):
        pytest.skip("filesystem doesn't support symlinks")

    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    # Plant the symlink-as-tray BEFORE the dispatcher sees its first event.
    (watch_root / "_unsorted").symlink_to(outside)

    src = watch_root / "evidence.txt"
    _drop(src)

    config = _config(watch_root, data_dir=tmp_path / "data")
    llm = RecordedLLM(
        [
            LLMResponse(destination=Path("Reports"), confidence=0.10, reason="low"),
        ]
    )
    outbound = FakeNotifierOutbound()
    deps = _make_deps(
        config=config,
        events=[_event(src, watch_root)],
        llm=llm,
        outbound=outbound,
    )
    await Dispatcher(deps).run()

    # File stays at its original location; nothing is moved into the
    # symlinked target outside the watch root.
    assert src.exists()
    assert not list(outside.iterdir())
    # And no notifier prompt is generated for the escape attempt.
    assert outbound.sent == []


async def test_apply_response_recovers_when_parked_file_missing(tmp_path: Path) -> None:
    """NEW-H1 regression: simulate a crash after ``move`` but before
    ``pending_log.transition``.

    Sequence:
      1. Park a file (one dispatcher run, LLM low-confidence response).
      2. Out-of-band, move the parked file to its target destination -
         this stands in for the "previous _apply_response moved it
         then crashed" state. The pending entry is still ``REQUESTED``.
      3. Run the dispatcher with an APPROVE response for that
         decision_id.

    Before the fix the replay attempted ``shutil.move`` on the now-
    missing source, raised :class:`FileSystemError`, the inbound loop
    logged ``notifier_apply_failed``, and the pending entry stayed
    ``REQUESTED`` forever. The fix: detect the missing source,
    transition to ``APPLIED``, advance the loop.
    """
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    (watch_root / "Reports").mkdir()
    src = watch_root / "evidence.txt"
    _drop(src)

    config = _config(watch_root, data_dir=tmp_path / "data")
    pending_log = JsonlPendingLog(config.data_dir / "pending_decisions.jsonl")
    decision_log = JsonlDecisionLog(config.data_dir / "decisions.jsonl")
    outbound = FakeNotifierOutbound()

    # 1. Park the file via a low-confidence LLM response that proposes
    #    Reports/ - this is the destination the user will later approve.
    llm = RecordedLLM([LLMResponse(destination=Path("Reports"), confidence=0.4, reason="maybe")])
    park_deps = _make_deps(
        config=config,
        events=[_event(src, watch_root)],
        llm=llm,
        outbound=outbound,
        pending_log=pending_log,
        decision_log=decision_log,
    )
    await Dispatcher(park_deps).run()

    parked = watch_root / "_unsorted" / "evidence.txt"
    assert parked.exists()
    decision_id = str(outbound.sent[0]["decision_id"])

    # 2. Simulate a crash mid-apply: move the parked file to its
    #    destination but leave the pending entry as REQUESTED.
    moved = watch_root / "Reports" / "evidence.txt"
    parked.rename(moved)
    assert not parked.exists()
    assert moved.exists()
    pending_before = await pending_log.get(decision_id)
    assert pending_before is not None
    assert pending_before.state is PendingState.REQUESTED

    # 3. Replay an APPROVE: with the fix, the pending entry must
    #    converge to APPLIED. Without the fix this raises
    #    FileSystemError inside _apply_response, which the inbound
    #    loop swallows - and the pending entry stays REQUESTED.
    inbound = FakeNotifierInbound(
        [
            NotifierResponse(
                decision_id=decision_id,
                kind=NotifierResponseKind.APPROVE,
                raw_text=f"/approve {decision_id}",
            )
        ]
    )
    replay_deps = _make_deps(
        config=config,
        events=[],
        llm=RecordedLLM([]),
        outbound=outbound,
        inbound=inbound,
        pending_log=pending_log,
        decision_log=decision_log,
    )
    await Dispatcher(replay_deps).run()

    pending_after = await pending_log.get(decision_id)
    assert pending_after is not None
    assert pending_after.state is PendingState.APPLIED
    # File stays where the prior "crash" left it.
    assert moved.exists()
    # No phantom duplicate at Reports/evidence (2).txt either.
    assert not (watch_root / "Reports" / "evidence (2).txt").exists()


class _CrashingPendingLog:
    """Wraps a real :class:`JsonlPendingLog`; raises after the Nth ``append``.

    The first ``append`` is allowed to commit so the durable record
    exists on disk; the Nth call raises :class:`FileSystemError`
    *after* the inner append has flushed, simulating a process death
    between the durable write and the subsequent move.
    """

    def __init__(self, inner: JsonlPendingLog, *, crash_on_append_n: int) -> None:
        self._inner = inner
        self._appends_seen = 0
        self._crash_after = crash_on_append_n

    async def append(self, pending):  # type: ignore[no-untyped-def]
        self._appends_seen += 1
        await self._inner.append(pending)
        if self._appends_seen == self._crash_after:
            msg = "simulated crash between pending_log.append and the move"
            raise FileSystemError(msg)

    async def transition(self, decision_id, *, to):  # type: ignore[no-untyped-def]
        return await self._inner.transition(decision_id, to=to)

    async def get(self, decision_id):  # type: ignore[no-untyped-def]
        return await self._inner.get(decision_id)

    async def replay(self):  # type: ignore[no-untyped-def]
        async for entry in self._inner.replay():
            yield entry

    async def replay_all(self):  # type: ignore[no-untyped-def]
        async for entry in self._inner.replay_all():
            yield entry


async def test_park_crash_between_pending_append_and_move_is_recoverable(
    tmp_path: Path,
) -> None:
    """Sev-2 regression: pin the new ``pending → move`` ordering + orphan recovery.

    Sequence:
      1. Dispatcher receives one event for a low-confidence file.
         ``_park_and_notify`` precomputes the unsorted target, writes
         the durable ``pending_log`` entry, and then crashes during
         the simulated post-append failure.
      2. The watch loop's broad-except catches the
         ``FileSystemError`` so the daemon keeps running.
      3. On a fresh dispatcher run, ``_recover_orphan_pending`` finds
         the REQUESTED entry whose ``unsorted_path`` doesn't exist
         and transitions it to APPLIED.

    Failure mode the new ordering closes: the old ``move → pending``
    sequence would have left the file in ``_unsorted/`` with no
    pending record, invisible to the inbound loop forever.
    """
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    src = watch_root / "evidence.txt"
    _drop(src)

    config = _config(watch_root, data_dir=tmp_path / "data")
    inner_pending = JsonlPendingLog(config.data_dir / "pending_decisions.jsonl")
    crashing_pending = _CrashingPendingLog(inner_pending, crash_on_append_n=1)
    decision_log = JsonlDecisionLog(config.data_dir / "decisions.jsonl")
    outbound = FakeNotifierOutbound()

    llm = RecordedLLM([LLMResponse(destination=Path("Reports"), confidence=0.40, reason="unsure")])
    crashing_deps = _make_deps(
        config=config,
        events=[_event(src, watch_root)],
        llm=llm,
        outbound=outbound,
        pending_log=crashing_pending,  # type: ignore[arg-type]
        decision_log=decision_log,
    )
    await Dispatcher(crashing_deps).run()

    # After the simulated crash:
    #   - the durable pending entry IS on disk
    #   - the file was NOT moved (the crash interrupted the sequence
    #     between pending append and move)
    #   - the source file still exists at its original path
    assert src.exists(), "source file must survive a pending-then-crash"
    pending_entries = [p async for p in inner_pending.replay()]
    assert len(pending_entries) == 1
    assert pending_entries[0].state is PendingState.REQUESTED
    parked_intended = pending_entries[0].unsorted_path
    assert not parked_intended.exists(), (
        "the intended parked path must NOT exist - the move never happened"
    )

    # No notifier was called (the crash interrupted before the
    # decision_log + outbound notify steps).
    assert outbound.sent == []

    # Now simulate a fresh dispatcher run. The orphan-recovery scan
    # must transition the dangling REQUESTED entry to APPLIED.
    recovery_pending = JsonlPendingLog(config.data_dir / "pending_decisions.jsonl")
    recovery_deps = _make_deps(
        config=config,
        events=[],
        llm=RecordedLLM([]),
        outbound=FakeNotifierOutbound(),
        pending_log=recovery_pending,
        decision_log=decision_log,
    )
    await Dispatcher(recovery_deps).run()

    recovered = await recovery_pending.get(pending_entries[0].decision_id)
    assert recovered is not None
    assert recovered.state is PendingState.APPLIED


async def test_orphan_recovery_skips_pending_with_existing_parked_file(
    tmp_path: Path,
) -> None:
    """Healthy REQUESTED entries with a live parked file are left alone.

    The recovery scan must not over-promote: an entry whose
    ``unsorted_path`` exists is a normal in-flight decision and
    must stay in ``REQUESTED`` so the user can respond.
    """
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    src = watch_root / "f.txt"
    _drop(src)

    config = _config(watch_root, data_dir=tmp_path / "data")
    pending_log = JsonlPendingLog(config.data_dir / "pending_decisions.jsonl")
    decision_log = JsonlDecisionLog(config.data_dir / "decisions.jsonl")
    outbound = FakeNotifierOutbound()

    # Run once to park the file normally.
    llm = RecordedLLM([LLMResponse(destination=Path("Reports"), confidence=0.40, reason="unsure")])
    park_deps = _make_deps(
        config=config,
        events=[_event(src, watch_root)],
        llm=llm,
        outbound=outbound,
        pending_log=pending_log,
        decision_log=decision_log,
    )
    await Dispatcher(park_deps).run()

    parked = watch_root / "_unsorted" / "f.txt"
    assert parked.exists()
    decision_id = str(outbound.sent[0]["decision_id"])
    before = await pending_log.get(decision_id)
    assert before is not None and before.state is PendingState.REQUESTED

    # Start a fresh dispatcher; the orphan-recovery scan must NOT
    # touch the healthy entry.
    recovery_pending = JsonlPendingLog(config.data_dir / "pending_decisions.jsonl")
    recovery_deps = _make_deps(
        config=config,
        events=[],
        llm=RecordedLLM([]),
        outbound=FakeNotifierOutbound(),
        pending_log=recovery_pending,
        decision_log=decision_log,
    )
    await Dispatcher(recovery_deps).run()

    after = await recovery_pending.get(decision_id)
    assert after is not None
    assert after.state is PendingState.REQUESTED


async def test_apply_response_filesystem_error_does_not_kill_inbound_loop(
    tmp_path: Path,
) -> None:
    """Test L: a FileSystemError from a destination must not tear the loop down.

    Park a file via the normal path, then issue an APPROVE
    response that resolves to a destination_root the filesystem
    refuses to mkdir into (we simulate this with a destination
    whose parent is a file - mkdir will raise OSError, which
    LocalFilesystem wraps as FileSystemError). The inbound loop's
    inner ``except TaxonomaidError`` should log
    ``notifier_apply_failed`` and continue draining; the pending
    entry stays REQUESTED so a future reply can retry.
    """
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    src = watch_root / "f.txt"
    _drop(src)
    # Plant a file where a future mkdir would otherwise create a
    # directory. ``LocalFilesystem.mkdir`` will fail with OSError
    # → FileSystemError.
    blocker = watch_root / "Reports"
    blocker.write_bytes(b"i am a file, not a directory")

    config = _config(watch_root, data_dir=tmp_path / "data")
    pending_log = JsonlPendingLog(config.data_dir / "pending_decisions.jsonl")
    decision_log = JsonlDecisionLog(config.data_dir / "decisions.jsonl")
    outbound = FakeNotifierOutbound()

    llm = RecordedLLM([LLMResponse(destination=Path("Reports"), confidence=0.4, reason="maybe")])
    park_deps = _make_deps(
        config=config,
        events=[_event(src, watch_root)],
        llm=llm,
        outbound=outbound,
        pending_log=pending_log,
        decision_log=decision_log,
    )
    await Dispatcher(park_deps).run()
    decision_id = str(outbound.sent[0]["decision_id"])

    # PROPOSE the same destination explicitly; the dispatcher
    # will safe_resolve it (allowed - it's under destination_root)
    # then attempt to mkdir into Reports/ and fail.
    inbound = FakeNotifierInbound(
        [
            NotifierResponse(
                decision_id=decision_id,
                kind=NotifierResponseKind.PROPOSE,
                proposed_destination=Path("Reports"),
                raw_text=f"/move {decision_id} Reports",
            )
        ]
    )
    apply_deps = _make_deps(
        config=config,
        events=[],
        llm=RecordedLLM([]),
        outbound=outbound,
        inbound=inbound,
        pending_log=pending_log,
        decision_log=decision_log,
    )
    # The loop must NOT raise to its caller: the inner except
    # swallows the FileSystemError and the loop drains the stream.
    await Dispatcher(apply_deps).run()

    pending_after = await pending_log.get(decision_id)
    assert pending_after is not None
    # The pending entry stays REQUESTED so a corrected reply can retry.
    assert pending_after.state is PendingState.REQUESTED


async def test_novel_destination_falls_through_to_park_even_at_high_confidence(
    tmp_path: Path,
) -> None:
    """1.1.b regression: a destination not in the candidate set can't auto-create.

    Without the candidate-set post-validation, a prompt-injection
    attack that convinces the LLM to report ``confidence=0.99`` and
    a novel ``Finance/Taxes/2099`` destination would auto-create
    the folder and place the file inside it. With the gate, the
    move falls through to park unless the destination has a parent
    that the user already curated.
    """
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    # Only ``Reports/`` exists as a candidate; ``Career/`` does not.
    (watch_root / "Reports").mkdir()
    src = watch_root / "f.pdf"
    _drop(src)

    config = _config(watch_root, data_dir=tmp_path / "data")
    outbound = FakeNotifierOutbound()
    llm = RecordedLLM(
        [
            LLMResponse(
                destination=Path("Career/CVs"),  # NOT in candidates
                confidence=0.99,  # would clear auto_create_folder=0.85
                reason="prompt-injected confidence",
            )
        ]
    )
    deps = _make_deps(
        config=config,
        events=[_event(src, watch_root)],
        llm=llm,
        outbound=outbound,
    )
    await Dispatcher(deps).run()

    # File must be parked, not placed at Career/CVs/.
    parked = watch_root / "_unsorted" / "f.pdf"
    assert parked.exists()
    assert not (watch_root / "Career").exists(), (
        "the dispatcher MUST NOT auto-create a folder outside the candidate set"
    )
    assert len(outbound.sent) == 1


async def test_destination_inside_known_parent_still_auto_creates(
    tmp_path: Path,
) -> None:
    """Auto-create is allowed when a parent of the proposal is a candidate.

    ``Career/CVs/2026`` is novel, but ``Career`` already exists -
    the LLM is extending a curated subtree, which is exactly the
    case auto_create_folder was designed for. We don't want to
    over-rotate and force park on every nested extension.
    """
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    (watch_root / "Career").mkdir()
    src = watch_root / "f.pdf"
    _drop(src)

    config = _config(watch_root, data_dir=tmp_path / "data")
    outbound = FakeNotifierOutbound()
    llm = RecordedLLM(
        [
            LLMResponse(
                destination=Path("Career/CVs/2026"),
                confidence=0.91,
                reason="CV in a known subtree",
            )
        ]
    )
    deps = _make_deps(
        config=config,
        events=[_event(src, watch_root)],
        llm=llm,
        outbound=outbound,
    )
    await Dispatcher(deps).run()

    moved = watch_root / "Career" / "CVs" / "2026" / "f.pdf"
    assert moved.exists()
    assert outbound.sent == []


# ---- /review session walk-through (Tier 3 Telegram review flow) ----


async def test_review_session_walks_pending_proposals_via_telegram(
    tmp_path: Path,
) -> None:
    """End-to-end: /review → proposal → approve → next → reject → done.

    Reproduces the operator's mobile experience: type ``/review``,
    tap Approve on proposal 1, tap Reject on proposal 2, see the
    "review complete" summary. After the run, ``rules.yaml`` has
    one new entry, ``rejected_rules.yaml`` has one, and
    ``proposed_rules.yaml`` is empty.
    """
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = _config(watch_root, data_dir=tmp_path / "data")

    paths = ReviewPaths.under(config_dir)
    a = Rule(
        id="alpha",
        match=MatchSpec(filename_regex=r"(?i)alpha"),
        destination_template="A/",
        weight=0.7,
        confidence=0.95,
        anchored=False,
        source=RuleSource.USER_INFERRED,
        sample_count=20,
    )
    b = Rule(
        id="beta",
        match=MatchSpec(filename_regex=r"(?i)beta"),
        destination_template="B/",
        weight=0.7,
        confidence=0.91,
        anchored=False,
        source=RuleSource.USER_INFERRED,
        sample_count=15,
    )
    write_rules_file(paths.proposed, (a, b))
    review_session = ReviewSession(paths)

    outbound = FakeNotifierOutbound()
    inbound = FakeNotifierInbound(
        [
            NotifierResponse(
                decision_id="",
                kind=NotifierResponseKind.REVIEW_START,
                raw_text="/review",
            ),
            NotifierResponse(
                decision_id="alpha",
                kind=NotifierResponseKind.RULE_APPROVE,
                raw_text="rule_approve:alpha",
            ),
            NotifierResponse(
                decision_id="beta",
                kind=NotifierResponseKind.RULE_REJECT,
                raw_text="rule_reject:beta",
            ),
        ]
    )
    deps = _make_deps(
        config=config,
        events=[],
        llm=RecordedLLM([]),
        outbound=outbound,
        inbound=inbound,
        review_session=review_session,
    )
    await Dispatcher(deps).run()

    # Three outbound proposals/messages: one per inbound event.
    # First two are rule proposals (sent in response to /review and
    # the approve tap). Third is the "review complete" summary
    # because after reject:beta the queue is empty.
    assert len(outbound.rule_proposals) == 2
    assert outbound.rule_proposals[0]["proposal"] is a or any(  # type: ignore[comparison-overlap]
        r["proposal"].id == "alpha"
        for r in outbound.rule_proposals  # type: ignore[union-attr]
    )
    assert len(outbound.review_completes) == 1
    assert outbound.review_completes[0]["approved"] == 1
    assert outbound.review_completes[0]["rejected"] == 1

    # On-disk state: alpha promoted, beta rejected, queue empty.
    queue = review_session.queue()
    assert queue.is_empty
    assert queue.approved_count == 1
    assert queue.rejected_count == 1


async def test_review_response_dropped_when_session_unwired(tmp_path: Path) -> None:
    """A ``/review`` arriving with no review_session is logged + ignored.

    Defence in depth: review-control responses must never leak into
    the per-file ``_apply_response`` path (which would treat the
    empty ``decision_id`` as ``notifier_unknown_decision``).
    """
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    config = _config(watch_root, data_dir=tmp_path / "data")

    outbound = FakeNotifierOutbound()
    inbound = FakeNotifierInbound(
        [
            NotifierResponse(
                decision_id="",
                kind=NotifierResponseKind.REVIEW_START,
                raw_text="/review",
            ),
        ]
    )
    deps = _make_deps(
        config=config,
        events=[],
        llm=RecordedLLM([]),
        outbound=outbound,
        inbound=inbound,
        review_session=None,  # Explicit: review unwired.
    )
    await Dispatcher(deps).run()

    # No proposals or completes were sent (the review session was
    # the bridge, and it's missing).
    assert outbound.rule_proposals == []
    assert outbound.review_completes == []
    # And the response did not leak into the per-file path: no
    # ``notify_pending`` call either.
    assert outbound.sent == []


# ---- LLM circuit breaker (#1 set-and-forget posture) -----------------


async def test_llm_circuit_trips_open_after_threshold_failures(
    tmp_path: Path,
) -> None:
    """3 consecutive LLM errors trip the circuit; the 3rd file is parked silent.

    Files 1-2: per-file Telegram prompt as before (one-off errors,
    unchanged UX). File 3: trips the circuit, sends ONE
    "LLM unavailable" alert, parks silently.
    """

    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    files = [watch_root / f"f{i}.pdf" for i in range(3)]
    for f in files:
        _drop(f)

    class AlwaysFailingLLM:
        async def classify(
            self,
            *,
            filename: str,
            excerpt: str,
            candidate_destinations: tuple[Path, ...],
            prior_user_moves: tuple[tuple[str, Path], ...] = (),
        ) -> LLMResponse:
            del filename, excerpt, candidate_destinations, prior_user_moves
            msg = "rate limited"
            raise LLMError(msg)

    config = _config(watch_root, data_dir=tmp_path / "data")
    outbound = FakeNotifierOutbound()
    circuit = LLMCircuit(threshold=3, cooldown_s=60.0)
    deps = DispatcherDeps(
        config=config,
        rule_engine=RuleEngine(rules=()),
        llm=AlwaysFailingLLM(),
        notifier_outbound=outbound,
        notifier_inbound=None,
        filesystem=LocalFilesystem(),
        decision_log=JsonlDecisionLog(config.data_dir / "decisions.jsonl"),
        pending_log=JsonlPendingLog(config.data_dir / "pending_decisions.jsonl"),
        watcher=FakeWatcher([_event(f, watch_root) for f in files]),
        clock=SystemClock(),
        debounce_s=0.0,
        llm_circuit=circuit,
    )
    await Dispatcher(deps).run()

    # 3 files were parked; only files 1 and 2 produced per-file
    # prompts (file 3 tripped the circuit and was parked silently).
    assert len(outbound.sent) == 2
    # Exactly one "circuit opened" alert.
    assert len(outbound.circuit_opens) == 1
    assert "rate limited" in outbound.circuit_opens[0]["reason"]  # type: ignore[index]
    # Circuit is now open.

    assert circuit.state is CircuitState.OPEN


async def test_llm_circuit_recovers_after_cooldown_and_reports_skipped(
    tmp_path: Path,
) -> None:
    """When the LLM comes back, ONE "recovered" alert reports the skipped count.

    Set up: circuit pre-opened with skipped_count=4. The LLM
    returns a successful classification. After processing, the
    circuit should close, report 4 files parked silently, and the
    file at hand should be auto-moved normally.
    """

    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    (watch_root / "Reports").mkdir()
    src = watch_root / "evidence.pdf"
    _drop(src)

    config = _config(watch_root, data_dir=tmp_path / "data")
    outbound = FakeNotifierOutbound()

    # Pre-open the circuit. It tripped 70 s ago (past the 60 s
    # cooldown), and 4 files were silently parked while open.
    circuit = LLMCircuit(threshold=3, cooldown_s=60.0)
    circuit.state = CircuitState.OPEN
    circuit.consecutive_failures = 3
    circuit.last_open_ts = datetime.now(tz=UTC) - timedelta(seconds=70)
    circuit.skipped_count = 4

    llm = RecordedLLM(
        [LLMResponse(destination=Path("Reports"), confidence=0.95, reason="recovered")]
    )

    deps = DispatcherDeps(
        config=config,
        rule_engine=RuleEngine(rules=()),
        llm=llm,
        notifier_outbound=outbound,
        notifier_inbound=None,
        filesystem=LocalFilesystem(),
        decision_log=JsonlDecisionLog(config.data_dir / "decisions.jsonl"),
        pending_log=JsonlPendingLog(config.data_dir / "pending_decisions.jsonl"),
        watcher=FakeWatcher([_event(src, watch_root)]),
        clock=SystemClock(),
        debounce_s=0.0,
        llm_circuit=circuit,
    )
    await Dispatcher(deps).run()

    # The successful classification closed the circuit.
    assert circuit.state is CircuitState.CLOSED
    assert circuit.skipped_count == 0
    # One recovery alert was sent reporting the 4 skipped files.
    assert len(outbound.circuit_recovers) == 1
    assert outbound.circuit_recovers[0]["skipped_files"] == 4
    # The file was moved (the LLM response was honoured).
    assert (watch_root / "Reports" / "evidence.pdf").exists()


async def test_llm_circuit_blocks_calls_inside_cooldown(tmp_path: Path) -> None:
    """While open and inside cooldown, the LLM is never called.

    Files arriving during the cooldown are parked silently with
    reason="LLM circuit open" - no per-file Telegram prompt, no
    LLM call burned.
    """

    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    src = watch_root / "f.pdf"
    _drop(src)

    config = _config(watch_root, data_dir=tmp_path / "data")
    outbound = FakeNotifierOutbound()

    # Circuit just opened (well within the cooldown).
    circuit = LLMCircuit(threshold=3, cooldown_s=60.0)
    circuit.state = CircuitState.OPEN
    circuit.consecutive_failures = 3
    circuit.last_open_ts = datetime.now(tz=UTC)
    circuit.skipped_count = 0

    llm = RecordedLLM([])  # Asserts no LLM calls happen.
    deps = DispatcherDeps(
        config=config,
        rule_engine=RuleEngine(rules=()),
        llm=llm,
        notifier_outbound=outbound,
        notifier_inbound=None,
        filesystem=LocalFilesystem(),
        decision_log=JsonlDecisionLog(config.data_dir / "decisions.jsonl"),
        pending_log=JsonlPendingLog(config.data_dir / "pending_decisions.jsonl"),
        watcher=FakeWatcher([_event(src, watch_root)]),
        clock=SystemClock(),
        debounce_s=0.0,
        llm_circuit=circuit,
    )
    await Dispatcher(deps).run()

    # No LLM call.
    assert llm.calls == []
    # File was parked silently (no per-file Telegram prompt).
    assert outbound.sent == []
    # And the silent skip was tracked.
    assert circuit.skipped_count == 1
    # File ended up in _unsorted/.
    assert (watch_root / "_unsorted" / "f.pdf").exists()


# ---- Bootstrap of pre-existing files (#1c set-and-forget posture) ---


async def test_bootstrap_existing_processes_files_already_in_watch_root(
    tmp_path: Path,
) -> None:
    """``bootstrap_existing=true`` walks the watch on startup.

    Models the first-deploy case: files exist in the watch root
    *before* the daemon starts listening on inotify. Without the
    bootstrap, those files would sit there forever invisible to
    the daemon.
    """
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    (watch_root / "Reports").mkdir()
    pre_existing = [
        watch_root / "doc-1.pdf",
        watch_root / "doc-2.pdf",
        watch_root / "subfolder" / "doc-3.pdf",
    ]
    for f in pre_existing:
        _drop(f)

    config = AppConfig(
        watches=WatchesConfig(
            watches=(
                WatchConfig(
                    path=watch_root,
                    destination_root=watch_root,
                    bootstrap_existing=True,
                ),
            ),
        ),
        llm=LLMConfig(
            api_key="x",
            thresholds=Thresholds(
                auto_move=0.75,
                auto_create_folder=0.85,
                auto_promote_rule=0.97,
            ),
        ),
        notifier=NotifierConfig(),
        data_dir=tmp_path / "data",
    )

    llm = RecordedLLM(
        [
            LLMResponse(destination=Path("Reports"), confidence=0.95, reason="r"),
            LLMResponse(destination=Path("Reports"), confidence=0.95, reason="r"),
            LLMResponse(destination=Path("Reports"), confidence=0.95, reason="r"),
        ],
    )
    deps = DispatcherDeps(
        config=config,
        rule_engine=RuleEngine(rules=()),
        llm=llm,
        notifier_outbound=None,
        notifier_inbound=None,
        filesystem=LocalFilesystem(),
        decision_log=JsonlDecisionLog(config.data_dir / "decisions.jsonl"),
        pending_log=JsonlPendingLog(config.data_dir / "pending_decisions.jsonl"),
        watcher=FakeWatcher([]),  # No new events; bootstrap does the work.
        clock=SystemClock(),
        debounce_s=0.0,
    )
    await Dispatcher(deps).run()

    # All three pre-existing files were classified by the LLM and moved.
    assert len(llm.calls) == 3
    for f in pre_existing:
        assert not f.exists(), f"{f} should have been moved out of the watch root"
    assert (watch_root / "Reports" / "doc-1.pdf").exists()
    assert (watch_root / "Reports" / "doc-2.pdf").exists()
    assert (watch_root / "Reports" / "doc-3.pdf").exists()


async def test_bootstrap_existing_skips_files_in_unsorted(tmp_path: Path) -> None:
    """The bootstrap walk skips files already parked in ``_unsorted/``.

    Re-classifying them would double-emit pending entries for files
    the operator has already been asked about. ``_recover_orphan_pending``
    is the right path for those.
    """
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    (watch_root / "_unsorted").mkdir()
    parked = watch_root / "_unsorted" / "already-parked.pdf"
    fresh = watch_root / "fresh.pdf"
    _drop(parked)
    _drop(fresh)

    config = AppConfig(
        watches=WatchesConfig(
            watches=(
                WatchConfig(
                    path=watch_root,
                    destination_root=watch_root,
                    bootstrap_existing=True,
                ),
            ),
        ),
        llm=LLMConfig(
            api_key="x",
            thresholds=Thresholds(
                auto_move=0.75,
                auto_create_folder=0.85,
                auto_promote_rule=0.97,
            ),
        ),
        notifier=NotifierConfig(),
        data_dir=tmp_path / "data",
    )
    llm = RecordedLLM(
        [LLMResponse(destination=Path("Reports"), confidence=0.95, reason="r")],
    )
    deps = DispatcherDeps(
        config=config,
        rule_engine=RuleEngine(rules=()),
        llm=llm,
        notifier_outbound=None,
        notifier_inbound=None,
        filesystem=LocalFilesystem(),
        decision_log=JsonlDecisionLog(config.data_dir / "decisions.jsonl"),
        pending_log=JsonlPendingLog(config.data_dir / "pending_decisions.jsonl"),
        watcher=FakeWatcher([]),
        clock=SystemClock(),
        debounce_s=0.0,
    )
    await Dispatcher(deps).run()

    # Only the fresh file went through the LLM. Parked file is untouched.
    assert len(llm.calls) == 1
    assert llm.calls[0][0] == "fresh.pdf"  # tuple[filename, excerpt]
    assert parked.exists()


async def test_bootstrap_existing_off_by_default_processes_nothing(
    tmp_path: Path,
) -> None:
    """Without ``bootstrap_existing=true`` pre-existing files are ignored.

    Otherwise a daemon restart on a busy folder would re-classify
    thousands of files - exactly what we want to avoid.
    """
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    _drop(watch_root / "doc.pdf")

    config = _config(watch_root, data_dir=tmp_path / "data")  # default flag = False
    llm = RecordedLLM([])
    deps = DispatcherDeps(
        config=config,
        rule_engine=RuleEngine(rules=()),
        llm=llm,
        notifier_outbound=None,
        notifier_inbound=None,
        filesystem=LocalFilesystem(),
        decision_log=JsonlDecisionLog(config.data_dir / "decisions.jsonl"),
        pending_log=JsonlPendingLog(config.data_dir / "pending_decisions.jsonl"),
        watcher=FakeWatcher([]),
        clock=SystemClock(),
        debounce_s=0.0,
    )
    await Dispatcher(deps).run()

    assert llm.calls == []
    assert (watch_root / "doc.pdf").exists()
