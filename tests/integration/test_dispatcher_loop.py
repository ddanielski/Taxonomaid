"""End-to-end dispatcher tests using fake adapters and a tmp_path filesystem."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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
)
from taxonomaid.domain import (
    DecisionSource,
    FileEvent,
    FileEventKind,
    PendingState,
)
from taxonomaid.ports import LLMResponse, NotifierResponse, NotifierResponseKind
from taxonomaid.services.dispatcher import Dispatcher, DispatcherDeps
from taxonomaid.services.rule_engine import RuleEngine
from tests.conftest import (
    FakeNotifierInbound,
    FakeNotifierOutbound,
    FakeWatcher,
    RecordedLLM,
)

pytestmark = pytest.mark.integration


@pytest.fixture()
def watch_setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    destination_root = watch_root
    unsorted = destination_root / "_unsorted"
    return watch_root, destination_root, unsorted


def _config(watch_root: Path, destination_root: Path, *, data_dir: Path) -> AppConfig:
    return AppConfig(
        watches=WatchesConfig(
            watches=(
                WatchConfig(
                    path=watch_root,
                    destination_root=destination_root,
                    recursive=True,
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


async def _collect_decisions(
    decision_log: JsonlDecisionLog,
) -> list[tuple[str, str, DecisionSource]]:
    out: list[tuple[str, str, DecisionSource]] = []
    async for entry in decision_log.replay():
        out.append((entry.decision_id, str(entry.destination), entry.source))
    return out


def _drop_file(path: Path, body: bytes = b"hello") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def _build_event(
    path: Path,
    *,
    watch_root: Path,
    destination_root: Path,
) -> FileEvent:
    return FileEvent(
        path=path,
        kind=FileEventKind.ADDED,
        watch_root=watch_root,
        destination_root=destination_root,
        unsorted_dir=Path("_unsorted"),
    )


async def _run_with_fakes(
    *,
    config: AppConfig,
    events: list[FileEvent],
    llm_responses: list[LLMResponse],
    notifier_responses: list[NotifierResponse] | None = None,
    extra_action: Callable[[Dispatcher], Awaitable[None]] | None = None,
) -> tuple[Dispatcher, RecordedLLM, FakeNotifierOutbound, FakeNotifierInbound | None]:
    llm = RecordedLLM(llm_responses)
    outbound = FakeNotifierOutbound()
    inbound: FakeNotifierInbound | None = (
        FakeNotifierInbound(notifier_responses) if notifier_responses is not None else None
    )

    deps = DispatcherDeps(
        config=config,
        rule_engine=RuleEngine(rules=()),
        llm=llm,
        notifier_outbound=outbound,
        notifier_inbound=inbound,
        filesystem=LocalFilesystem(),
        decision_log=JsonlDecisionLog(config.data_dir / "decisions.jsonl"),
        pending_log=JsonlPendingLog(config.data_dir / "pending_decisions.jsonl"),
        watcher=FakeWatcher(events),
        clock=SystemClock(),
        debounce_s=0.0,
    )
    dispatcher = Dispatcher(deps)
    await dispatcher.run()
    if extra_action is not None:
        await extra_action(dispatcher)
    return dispatcher, llm, outbound, inbound


async def test_high_confidence_moves_into_existing_destination(
    watch_setup: tuple[Path, Path, Path],
    tmp_path: Path,
) -> None:
    watch_root, destination_root, _unsorted = watch_setup
    (destination_root / "Finance").mkdir()
    src = watch_root / "tax_2025.pdf"
    _drop_file(src, b"IRS form")

    config = _config(watch_root, destination_root, data_dir=tmp_path / "data")
    events = [_build_event(src, watch_root=watch_root, destination_root=destination_root)]
    llm_responses = [
        LLMResponse(
            destination=Path("Finance"),
            confidence=0.92,
            reason="tax document",
        ),
    ]

    _, _llm, outbound, _inbound = await _run_with_fakes(
        config=config,
        events=events,
        llm_responses=llm_responses,
    )

    moved = destination_root / "Finance" / "tax_2025.pdf"
    assert moved.exists()
    assert not src.exists()
    assert outbound.sent == []

    decisions = await _collect_decisions(JsonlDecisionLog(config.data_dir / "decisions.jsonl"))
    assert len(decisions) == 1
    _did, dest, source = decisions[0]
    assert source is DecisionSource.LLM
    assert dest.endswith("Finance")


async def test_high_confidence_can_create_new_folder(
    watch_setup: tuple[Path, Path, Path],
    tmp_path: Path,
) -> None:
    watch_root, destination_root, _unsorted = watch_setup
    src = watch_root / "report.txt"
    _drop_file(src)

    config = _config(watch_root, destination_root, data_dir=tmp_path / "data")
    events = [_build_event(src, watch_root=watch_root, destination_root=destination_root)]
    llm_responses = [
        LLMResponse(
            destination=Path("Reports"),
            confidence=0.95,
            reason="status report",
        ),
    ]

    await _run_with_fakes(config=config, events=events, llm_responses=llm_responses)

    moved = destination_root / "Reports" / "report.txt"
    assert moved.exists()


async def test_low_confidence_parks_and_notifies(
    watch_setup: tuple[Path, Path, Path],
    tmp_path: Path,
) -> None:
    watch_root, destination_root, unsorted = watch_setup
    src = watch_root / "ambiguous.bin"
    _drop_file(src, b"???")

    config = _config(watch_root, destination_root, data_dir=tmp_path / "data")
    events = [_build_event(src, watch_root=watch_root, destination_root=destination_root)]
    llm_responses = [
        LLMResponse(
            destination=Path("Documents/Maybe"),
            confidence=0.4,
            reason="not sure",
        ),
    ]

    _, _llm, outbound, _inbound = await _run_with_fakes(
        config=config,
        events=events,
        llm_responses=llm_responses,
    )

    parked = unsorted / "ambiguous.bin"
    assert parked.exists()
    assert not src.exists()
    assert len(outbound.sent) == 1
    sent = outbound.sent[0]
    assert sent["file"] == parked
    assert sent["confidence"] == pytest.approx(0.4)


async def test_approve_reply_moves_from_unsorted(
    watch_setup: tuple[Path, Path, Path],
    tmp_path: Path,
) -> None:
    watch_root, destination_root, unsorted = watch_setup
    src = watch_root / "thing.pdf"
    _drop_file(src)

    config = _config(watch_root, destination_root, data_dir=tmp_path / "data")
    events = [_build_event(src, watch_root=watch_root, destination_root=destination_root)]
    llm_responses = [
        LLMResponse(
            destination=Path("Personal/Stuff"),
            confidence=0.4,
            reason="maybe stuff",
        ),
    ]
    pending_log = JsonlPendingLog(config.data_dir / "pending_decisions.jsonl")

    llm = RecordedLLM(llm_responses)
    outbound = FakeNotifierOutbound()

    deps = DispatcherDeps(
        config=config,
        rule_engine=RuleEngine(rules=()),
        llm=llm,
        notifier_outbound=outbound,
        notifier_inbound=None,
        filesystem=LocalFilesystem(),
        decision_log=JsonlDecisionLog(config.data_dir / "decisions.jsonl"),
        pending_log=pending_log,
        watcher=FakeWatcher(events),
        clock=SystemClock(),
        debounce_s=0.0,
    )
    dispatcher = Dispatcher(deps)
    await dispatcher.run()

    assert len(outbound.sent) == 1
    decision_id = str(outbound.sent[0]["decision_id"])
    parked = unsorted / "thing.pdf"
    assert parked.exists()

    inbound = FakeNotifierInbound(
        [
            NotifierResponse(
                decision_id=decision_id,
                kind=NotifierResponseKind.APPROVE,
                raw_text=f"/approve {decision_id}",
            )
        ]
    )
    deps_with_inbound = DispatcherDeps(
        config=config,
        rule_engine=RuleEngine(rules=()),
        llm=RecordedLLM([]),
        notifier_outbound=outbound,
        notifier_inbound=inbound,
        filesystem=LocalFilesystem(),
        decision_log=JsonlDecisionLog(config.data_dir / "decisions.jsonl"),
        pending_log=pending_log,
        watcher=FakeWatcher([]),
        clock=SystemClock(),
        debounce_s=0.0,
    )
    second = Dispatcher(deps_with_inbound)
    await second.run()

    moved = destination_root / "Personal" / "Stuff" / "thing.pdf"
    assert moved.exists()
    assert not parked.exists()

    pending = await pending_log.get(decision_id)
    assert pending is not None
    assert pending.state is PendingState.APPLIED


async def test_propose_reply_uses_user_path(
    watch_setup: tuple[Path, Path, Path],
    tmp_path: Path,
) -> None:
    watch_root, destination_root, unsorted = watch_setup
    src = watch_root / "thing.pdf"
    _drop_file(src)

    config = _config(watch_root, destination_root, data_dir=tmp_path / "data")
    events = [_build_event(src, watch_root=watch_root, destination_root=destination_root)]
    llm_responses = [
        LLMResponse(
            destination=Path("WrongFolder"),
            confidence=0.3,
            reason="guess",
        ),
    ]
    pending_log = JsonlPendingLog(config.data_dir / "pending_decisions.jsonl")
    decision_log = JsonlDecisionLog(config.data_dir / "decisions.jsonl")
    fs = LocalFilesystem()

    llm = RecordedLLM(llm_responses)
    outbound = FakeNotifierOutbound()
    deps = DispatcherDeps(
        config=config,
        rule_engine=RuleEngine(rules=()),
        llm=llm,
        notifier_outbound=outbound,
        notifier_inbound=None,
        filesystem=fs,
        decision_log=decision_log,
        pending_log=pending_log,
        watcher=FakeWatcher(events),
        clock=SystemClock(),
        debounce_s=0.0,
    )
    await Dispatcher(deps).run()
    decision_id = str(outbound.sent[0]["decision_id"])

    inbound = FakeNotifierInbound(
        [
            NotifierResponse(
                decision_id=decision_id,
                kind=NotifierResponseKind.PROPOSE,
                proposed_destination=Path("Correct/Path"),
                raw_text=f"/move {decision_id} Correct/Path",
            )
        ]
    )
    deps2 = DispatcherDeps(
        config=config,
        rule_engine=RuleEngine(rules=()),
        llm=RecordedLLM([]),
        notifier_outbound=outbound,
        notifier_inbound=inbound,
        filesystem=fs,
        decision_log=decision_log,
        pending_log=pending_log,
        watcher=FakeWatcher([]),
        clock=SystemClock(),
        debounce_s=0.0,
    )
    await Dispatcher(deps2).run()

    moved = destination_root / "Correct" / "Path" / "thing.pdf"
    assert moved.exists()
    parked = unsorted / "thing.pdf"
    assert not parked.exists()

    decisions = await _collect_decisions(decision_log)
    sources = [s for _, _, s in decisions]
    assert DecisionSource.USER_OVERRIDE in sources


async def test_reject_leaves_file_in_unsorted(
    watch_setup: tuple[Path, Path, Path],
    tmp_path: Path,
) -> None:
    watch_root, destination_root, unsorted = watch_setup
    src = watch_root / "thing.pdf"
    _drop_file(src)

    config = _config(watch_root, destination_root, data_dir=tmp_path / "data")
    events = [_build_event(src, watch_root=watch_root, destination_root=destination_root)]
    llm_responses = [
        LLMResponse(
            destination=Path("Foo"),
            confidence=0.3,
            reason="maybe foo",
        ),
    ]
    pending_log = JsonlPendingLog(config.data_dir / "pending_decisions.jsonl")
    decision_log = JsonlDecisionLog(config.data_dir / "decisions.jsonl")

    outbound = FakeNotifierOutbound()
    deps = DispatcherDeps(
        config=config,
        rule_engine=RuleEngine(rules=()),
        llm=RecordedLLM(llm_responses),
        notifier_outbound=outbound,
        notifier_inbound=None,
        filesystem=LocalFilesystem(),
        decision_log=decision_log,
        pending_log=pending_log,
        watcher=FakeWatcher(events),
        clock=SystemClock(),
        debounce_s=0.0,
    )
    await Dispatcher(deps).run()
    decision_id = str(outbound.sent[0]["decision_id"])

    inbound = FakeNotifierInbound(
        [
            NotifierResponse(
                decision_id=decision_id,
                kind=NotifierResponseKind.REJECT,
                raw_text=f"/reject {decision_id}",
            )
        ]
    )
    deps2 = DispatcherDeps(
        config=config,
        rule_engine=RuleEngine(rules=()),
        llm=RecordedLLM([]),
        notifier_outbound=outbound,
        notifier_inbound=inbound,
        filesystem=LocalFilesystem(),
        decision_log=decision_log,
        pending_log=pending_log,
        watcher=FakeWatcher([]),
        clock=SystemClock(),
        debounce_s=0.0,
    )
    await Dispatcher(deps2).run()

    parked = unsorted / "thing.pdf"
    assert parked.exists()
    pending = await pending_log.get(decision_id)
    assert pending is not None
    assert pending.state is PendingState.ANSWERED
