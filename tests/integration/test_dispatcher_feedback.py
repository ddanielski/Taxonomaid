"""Integration tests for the Phase-4 feedback loop and similarity bias."""

from __future__ import annotations

from datetime import UTC, datetime
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
    Decision,
    DecisionSource,
    FileEvent,
    FileEventKind,
)
from taxonomaid.ports import LLMResponse
from taxonomaid.services import RuleEngine, SimilarityIndex
from taxonomaid.services.dispatcher import Dispatcher, DispatcherDeps
from tests.conftest import FakeWatcher, RecordedLLM

pytestmark = pytest.mark.integration


def _config(watch_root: Path, *, data_dir: Path) -> AppConfig:
    return AppConfig(
        watches=WatchesConfig(
            watches=(WatchConfig(path=watch_root, destination_root=watch_root),),
        ),
        llm=LLMConfig(
            api_key="x",
            thresholds=Thresholds(auto_move=0.75, auto_create_folder=0.85),
        ),
        notifier=NotifierConfig(),
        data_dir=data_dir,
    )


def _added(path: Path, root: Path) -> FileEvent:
    return FileEvent(
        path=path,
        kind=FileEventKind.ADDED,
        watch_root=root,
        destination_root=root,
        unsorted_dir=Path("_unsorted"),
    )


def _deleted(path: Path, root: Path) -> FileEvent:
    return FileEvent(
        path=path,
        kind=FileEventKind.DELETED,
        watch_root=root,
        destination_root=root,
        unsorted_dir=Path("_unsorted"),
    )


def _drop(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")


async def test_self_triggered_added_events_are_ignored(tmp_path: Path) -> None:
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    (watch_root / "Foo").mkdir()
    src = watch_root / "thing.txt"
    _drop(src)

    placed = watch_root / "Foo" / "thing.txt"

    config = _config(watch_root, data_dir=tmp_path / "data")
    llm = RecordedLLM(
        [
            LLMResponse(destination=Path("Foo"), confidence=0.9, reason="match"),
        ]
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
        watcher=FakeWatcher(
            [
                _added(src, watch_root),
                _added(placed, watch_root),
            ]
        ),
        clock=SystemClock(),
        debounce_s=0.0,
    )
    await Dispatcher(deps).run()
    assert len(llm.calls) == 1


async def test_user_override_logs_negative_signal(tmp_path: Path) -> None:
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    (watch_root / "Foo").mkdir()
    src = watch_root / "thing.txt"
    _drop(src)

    config = _config(watch_root, data_dir=tmp_path / "data")
    llm = RecordedLLM(
        [
            LLMResponse(destination=Path("Foo"), confidence=0.9, reason="match"),
        ]
    )
    decision_log = JsonlDecisionLog(config.data_dir / "decisions.jsonl")
    deps = DispatcherDeps(
        config=config,
        rule_engine=RuleEngine(rules=()),
        llm=llm,
        notifier_outbound=None,
        notifier_inbound=None,
        filesystem=LocalFilesystem(),
        decision_log=decision_log,
        pending_log=JsonlPendingLog(config.data_dir / "pending_decisions.jsonl"),
        watcher=FakeWatcher(
            [
                _added(src, watch_root),
                _deleted(watch_root / "Foo" / "thing.txt", watch_root),
            ]
        ),
        clock=SystemClock(),
        debounce_s=0.0,
    )
    await Dispatcher(deps).run()

    sources: list[DecisionSource] = []
    async for entry in decision_log.replay():
        sources.append(entry.source)
    assert DecisionSource.USER_OVERRIDE in sources


async def test_similarity_index_is_warmed_from_decision_log(tmp_path: Path) -> None:
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    src = watch_root / "tax_2025.pdf"
    _drop(src)

    config = _config(watch_root, data_dir=tmp_path / "data")
    decision_log = JsonlDecisionLog(config.data_dir / "decisions.jsonl")
    historical = Decision(
        decision_id="legacy_a",
        ts=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
        file=Path("tax_2024.pdf"),
        destination=Path("Finance/Taxes/2024"),
        source=DecisionSource.LLM,
        confidence=0.92,
        reason="prior tax",
    )
    await decision_log.append(historical)

    captured: dict[str, object] = {}

    class CapturingLLM:
        async def classify(
            self,
            *,
            filename: str,
            excerpt: str,
            candidate_destinations: tuple[Path, ...],
            prior_user_moves: tuple[tuple[str, Path], ...] = (),
        ) -> LLMResponse:
            del excerpt, candidate_destinations
            captured["filename"] = filename
            captured["prior_user_moves"] = prior_user_moves
            return LLMResponse(
                destination=Path("Finance/Taxes/2025"),
                confidence=0.92,
                reason="similar to prior",
            )

    similarity = SimilarityIndex()
    deps = DispatcherDeps(
        config=config,
        rule_engine=RuleEngine(rules=()),
        llm=CapturingLLM(),
        notifier_outbound=None,
        notifier_inbound=None,
        filesystem=LocalFilesystem(),
        decision_log=decision_log,
        pending_log=JsonlPendingLog(config.data_dir / "pending_decisions.jsonl"),
        watcher=FakeWatcher([_added(src, watch_root)]),
        clock=SystemClock(),
        similarity=similarity,
        debounce_s=0.0,
    )
    await Dispatcher(deps).run()

    prior_user_moves = captured["prior_user_moves"]
    assert isinstance(prior_user_moves, tuple)
    assert any(name == "tax_2024.pdf" for name, _dest in prior_user_moves)


async def test_similarity_index_grows_after_each_placement(tmp_path: Path) -> None:
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    (watch_root / "Reports").mkdir()
    a = watch_root / "report_alpha.docx"
    b = watch_root / "report_beta.docx"
    _drop(a)
    _drop(b)

    similarity = SimilarityIndex()

    class StaticLLM:
        async def classify(
            self,
            *,
            filename: str,
            excerpt: str,
            candidate_destinations: tuple[Path, ...],
            prior_user_moves: tuple[tuple[str, Path], ...] = (),
        ) -> LLMResponse:
            del filename, excerpt, candidate_destinations, prior_user_moves
            return LLMResponse(
                destination=Path("Reports"),
                confidence=0.9,
                reason="report",
            )

    config = _config(watch_root, data_dir=tmp_path / "data")
    deps = DispatcherDeps(
        config=config,
        rule_engine=RuleEngine(rules=()),
        llm=StaticLLM(),
        notifier_outbound=None,
        notifier_inbound=None,
        filesystem=LocalFilesystem(),
        decision_log=JsonlDecisionLog(config.data_dir / "decisions.jsonl"),
        pending_log=JsonlPendingLog(config.data_dir / "pending_decisions.jsonl"),
        watcher=FakeWatcher([_added(a, watch_root), _added(b, watch_root)]),
        clock=SystemClock(),
        similarity=similarity,
        debounce_s=0.0,
    )
    await Dispatcher(deps).run()

    matches = similarity.top_matches("report_gamma.docx")
    files = {name for name, _dest in matches}
    assert "report_alpha.docx" in files
    assert "report_beta.docx" in files


async def test_similarity_bias_is_passed_to_llm(tmp_path: Path) -> None:
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    src = watch_root / "tax_2025.pdf"
    _drop(src)

    similarity = SimilarityIndex()
    similarity.add(filename="tax_2024.pdf", destination=Path("Finance/Taxes/2024"))

    captured: dict[str, object] = {}

    class CapturingLLM:
        async def classify(
            self,
            *,
            filename: str,
            excerpt: str,
            candidate_destinations: tuple[Path, ...],
            prior_user_moves: tuple[tuple[str, Path], ...] = (),
        ) -> LLMResponse:
            del excerpt, candidate_destinations
            captured["filename"] = filename
            captured["prior_user_moves"] = prior_user_moves
            return LLMResponse(
                destination=Path("Finance/Taxes/2025"),
                confidence=0.92,
                reason="similar to prior",
            )

    config = _config(watch_root, data_dir=tmp_path / "data")
    deps = DispatcherDeps(
        config=config,
        rule_engine=RuleEngine(rules=()),
        llm=CapturingLLM(),
        notifier_outbound=None,
        notifier_inbound=None,
        filesystem=LocalFilesystem(),
        decision_log=JsonlDecisionLog(config.data_dir / "decisions.jsonl"),
        pending_log=JsonlPendingLog(config.data_dir / "pending_decisions.jsonl"),
        watcher=FakeWatcher([_added(src, watch_root)]),
        clock=SystemClock(),
        similarity=similarity,
        debounce_s=0.0,
    )
    await Dispatcher(deps).run()

    prior_user_moves = captured["prior_user_moves"]
    assert isinstance(prior_user_moves, tuple)
    assert any(name == "tax_2024.pdf" for name, _dest in prior_user_moves)
