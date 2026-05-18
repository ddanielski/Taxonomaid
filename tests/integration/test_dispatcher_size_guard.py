"""Integration tests for the dispatcher's size cap and extraction timeout.

These prove the :data:`_MAX_INPUT_BYTES_FOR_EXTRACTION` and
:data:`_EXTRACTION_TIMEOUT_S` constants in
:mod:`taxonomaid.services.dispatcher` actually fire. We monkeypatch
the constants down to test-friendly values so the assertions can run
on small fixtures rather than 50 MiB files / 30 s sleeps.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from taxonomaid.adapters.clock import SystemClock
from taxonomaid.adapters.decision_log import JsonlDecisionLog
from taxonomaid.adapters.filesystem import LocalFilesystem
from taxonomaid.adapters.pending_log import JsonlPendingLog
from taxonomaid.config import (
    AppConfig,
    LLMConfig,
    NotifierConfig,
    WatchConfig,
    WatchesConfig,
)
from taxonomaid.domain import FileEvent, FileEventKind
from taxonomaid.ports import LLMResponse
from taxonomaid.services import RuleEngine
from taxonomaid.services.dispatcher import Dispatcher, DispatcherDeps
from tests.conftest import FakeNotifierOutbound, FakeWatcher

pytestmark = pytest.mark.integration


def _config(watch_root: Path, *, data_dir: Path) -> AppConfig:
    return AppConfig(
        watches=WatchesConfig(
            watches=(WatchConfig(path=watch_root, destination_root=watch_root),),
        ),
        llm=LLMConfig(api_key="x"),
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


class _CapturingLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def classify(
        self,
        *,
        filename: str,
        excerpt: str,
        candidate_destinations: tuple[Path, ...],
        prior_user_moves: tuple[tuple[str, Path], ...] = (),
    ) -> LLMResponse:
        del candidate_destinations, prior_user_moves
        self.calls.append({"filename": filename, "excerpt": excerpt})
        return LLMResponse(destination=Path("Foo"), confidence=0.5, reason="r")


async def test_oversized_file_skips_excerpt_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pretend the cap is 100 bytes; drop a 1 KiB file. The dispatcher
    # should classify on filename alone (empty excerpt).
    monkeypatch.setattr(
        "taxonomaid.services.dispatcher._MAX_INPUT_BYTES_FOR_EXTRACTION",
        100,
    )
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    src = watch_root / "huge.txt"
    src.write_bytes(b"x" * 1024)

    config = _config(watch_root, data_dir=tmp_path / "data")
    llm = _CapturingLLM()
    deps = DispatcherDeps(
        config=config,
        rule_engine=RuleEngine(rules=()),
        llm=llm,
        notifier_outbound=FakeNotifierOutbound(),
        notifier_inbound=None,
        filesystem=LocalFilesystem(),
        decision_log=JsonlDecisionLog(config.data_dir / "decisions.jsonl"),
        pending_log=JsonlPendingLog(config.data_dir / "pending_decisions.jsonl"),
        watcher=FakeWatcher([_event(src, watch_root)]),
        clock=SystemClock(),
        debounce_s=0.0,
    )
    await Dispatcher(deps).run()

    assert len(llm.calls) == 1
    assert llm.calls[0]["filename"] == "huge.txt"
    assert llm.calls[0]["excerpt"] == ""


async def test_extraction_timeout_falls_back_to_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the extractor hangs, the dispatcher classifies on filename alone."""
    # 50 ms timeout, extractor sleeps 500 ms.
    monkeypatch.setattr("taxonomaid.services.dispatcher._EXTRACTION_TIMEOUT_S", 0.05)

    def slow_read_excerpt(path: Path, *, max_chars: int) -> str:
        del path, max_chars
        time.sleep(0.5)
        return "this should never reach the LLM"

    monkeypatch.setattr(
        "taxonomaid.services.dispatcher.read_excerpt",
        slow_read_excerpt,
    )

    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    src = watch_root / "report.txt"
    src.write_bytes(b"hello")

    config = _config(watch_root, data_dir=tmp_path / "data")
    llm = _CapturingLLM()
    deps = DispatcherDeps(
        config=config,
        rule_engine=RuleEngine(rules=()),
        llm=llm,
        notifier_outbound=FakeNotifierOutbound(),
        notifier_inbound=None,
        filesystem=LocalFilesystem(),
        decision_log=JsonlDecisionLog(config.data_dir / "decisions.jsonl"),
        pending_log=JsonlPendingLog(config.data_dir / "pending_decisions.jsonl"),
        watcher=FakeWatcher([_event(src, watch_root)]),
        clock=SystemClock(),
        debounce_s=0.0,
    )
    await Dispatcher(deps).run()

    assert len(llm.calls) == 1
    assert llm.calls[0]["excerpt"] == ""
