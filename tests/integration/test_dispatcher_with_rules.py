"""Integration tests proving the rule engine fires before the LLM."""

from __future__ import annotations

from pathlib import Path

import pytest

from taxonomaid.adapters.clock import SystemClock
from taxonomaid.adapters.decision_log import JsonlDecisionLog
from taxonomaid.adapters.filesystem import LocalFilesystem
from taxonomaid.adapters.pending_log import JsonlPendingLog
from taxonomaid.bootstrap import build_app_from_paths
from taxonomaid.config import (
    AppConfig,
    LLMConfig,
    NotifierConfig,
    Thresholds,
    WatchConfig,
    WatchesConfig,
)
from taxonomaid.domain import (
    CoherenceSpec,
    DecisionSource,
    FileEvent,
    FileEventKind,
    MatchSpec,
    Rule,
)
from taxonomaid.ports import LLMResponse
from taxonomaid.services.dispatcher import Dispatcher, DispatcherDeps
from taxonomaid.services.rule_engine import RuleEngine
from tests.conftest import FakeNotifierOutbound, FakeWatcher, RecordedLLM

pytestmark = pytest.mark.integration


def _config(watch_root: Path, *, data_dir: Path) -> AppConfig:
    return AppConfig(
        watches=WatchesConfig(
            watches=(WatchConfig(path=watch_root, destination_root=watch_root),),
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


async def test_rule_match_routes_without_calling_llm(tmp_path: Path) -> None:
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    src = watch_root / "tax_2025.pdf"
    src.write_bytes(b"IRS")

    rule = Rule(
        id="tax_pdf_to_year",
        match=MatchSpec(filename_regex=r"(?i)tax", ext=(".pdf",)),
        destination_template="Finance/Taxes/{year}/",
        coherence=CoherenceSpec(year_match=True),
        anchored=True,
    )

    config = _config(watch_root, data_dir=tmp_path / "data")
    llm = RecordedLLM([])
    outbound = FakeNotifierOutbound()
    deps = DispatcherDeps(
        config=config,
        rule_engine=RuleEngine(rules=(rule,)),
        llm=llm,
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

    moved = watch_root / "Finance" / "Taxes" / "2025" / "tax_2025.pdf"
    assert moved.exists()
    assert llm.calls == []
    assert outbound.sent == []

    sources: list[DecisionSource] = []
    rule_ids: list[str | None] = []
    async for entry in JsonlDecisionLog(config.data_dir / "decisions.jsonl").replay():
        sources.append(entry.source)
        rule_ids.append(entry.rule_id)
    assert sources == [DecisionSource.RULE]
    assert rule_ids == ["tax_pdf_to_year"]


async def test_year_mismatch_falls_through_to_llm(tmp_path: Path) -> None:
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    src = watch_root / "tax_2025.pdf"
    src.write_bytes(b"IRS")

    rule = Rule(
        id="tax_2024_only",
        match=MatchSpec(filename_regex=r"(?i)tax", ext=(".pdf",)),
        destination_template="Finance/Taxes/2024/",
        coherence=CoherenceSpec(year_match=True),
    )

    config = _config(watch_root, data_dir=tmp_path / "data")

    llm = RecordedLLM(
        [
            LLMResponse(
                destination=Path("Finance/Taxes/2025"),
                confidence=0.9,
                reason="LLM caught the year",
            )
        ]
    )
    outbound = FakeNotifierOutbound()
    deps = DispatcherDeps(
        config=config,
        rule_engine=RuleEngine(rules=(rule,)),
        llm=llm,
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

    moved = watch_root / "Finance" / "Taxes" / "2025" / "tax_2025.pdf"
    assert moved.exists()
    assert len(llm.calls) == 1


def test_bootstrap_tolerates_missing_rules_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "x")
    watch_root = tmp_path / "watch"
    watch_root.mkdir()

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "watches.yaml").write_text(
        f"""
watches:
  - path: {watch_root}
    destination_root: {watch_root}
    rules_file: {config_dir / "rules.yaml"}
""",
        encoding="utf-8",
    )
    (config_dir / "llm.yaml").write_text("api_key: ${LLM_API_KEY}\n", encoding="utf-8")
    (config_dir / "notifier.yaml").write_text("apprise_urls: []\n", encoding="utf-8")

    application = build_app_from_paths(
        watches_path=config_dir / "watches.yaml",
        llm_path=config_dir / "llm.yaml",
        notifier_path=config_dir / "notifier.yaml",
        data_dir=tmp_path / "data",
    )
    engines = application.dispatcher.deps.rule_engines_by_root
    assert engines is not None
    assert engines[watch_root].rules == ()


async def test_dispatcher_loads_rules_from_yaml_via_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "x")
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(
        """
rules:
  - id: tax_pdf_to_year
    match:
      filename_regex: '(?i)tax'
      ext: [.pdf]
    destination_template: 'Finance/Taxes/{year}/'
    coherence:
      year_match: true
    anchored: true
""",
        encoding="utf-8",
    )

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "watches.yaml").write_text(
        f"""
watches:
  - path: {watch_root}
    destination_root: {watch_root}
    rules_file: {rules_file}
""",
        encoding="utf-8",
    )
    (config_dir / "llm.yaml").write_text("api_key: ${LLM_API_KEY}\n", encoding="utf-8")
    (config_dir / "notifier.yaml").write_text("apprise_urls: []\n", encoding="utf-8")

    app = build_app_from_paths(
        watches_path=config_dir / "watches.yaml",
        llm_path=config_dir / "llm.yaml",
        notifier_path=config_dir / "notifier.yaml",
        data_dir=tmp_path / "data",
    )

    engines = app.dispatcher.deps.rule_engines_by_root
    assert engines is not None
    rule_set = engines[watch_root].rules
    assert len(rule_set) == 1
    assert rule_set[0].id == "tax_pdf_to_year"
