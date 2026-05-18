"""Integration smoke tests: build an :class:`App` from disk and exercise it.

These are deliberately shallow in Phase 0 - the dispatcher itself raises
``NotImplementedError`` until Phase 1. They verify that:

* Real config YAML round-trips through validation.
* The composition root wires together every adapter / service.
* CLI entry points expose the expected commands.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from taxonomaid.bootstrap import build_app_from_paths
from taxonomaid.cli import app

pytestmark = pytest.mark.integration


def _write_configs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    watches_path = tmp_path / "watches.yaml"
    llm_path = tmp_path / "llm.yaml"
    notifier_path = tmp_path / "notifier.yaml"

    watch_dir = tmp_path / "test-watch"
    watch_dir.mkdir()

    watches_path.write_text(
        f"""
watches:
  - path: {watch_dir}
    destination_root: {watch_dir}
    recursive: true
""",
        encoding="utf-8",
    )
    llm_path.write_text("api_key: ${LLM_API_KEY}\n", encoding="utf-8")
    notifier_path.write_text("apprise_urls: []\n", encoding="utf-8")
    return watches_path, llm_path, notifier_path


def test_build_app_wires_dispatcher_with_all_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watches_path, llm_path, notifier_path = _write_configs(tmp_path, monkeypatch)

    application = build_app_from_paths(
        watches_path=watches_path,
        llm_path=llm_path,
        notifier_path=notifier_path,
        data_dir=tmp_path / "data",
    )

    deps = application.dispatcher.deps
    assert deps.config.llm.model == "gemini-3.1-flash-lite"
    assert deps.notifier_outbound is None
    assert deps.notifier_inbound is None
    assert deps.filesystem is not None
    assert deps.decision_log is not None
    assert deps.pending_log is not None
    assert deps.watcher is not None
    assert deps.clock is not None


def test_cli_doctor_reports_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    watches_path, _llm_path, _notifier_path = _write_configs(tmp_path, monkeypatch)
    config_dir = watches_path.parent

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "-c", str(config_dir), "-d", str(tmp_path / "data")])

    assert result.exit_code == 0, result.stdout
    assert "config OK" in result.stdout


def test_cli_doctor_reports_error_on_missing_file(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "-c", str(tmp_path / "missing")])
    assert result.exit_code == 1
    assert "config error" in result.stdout


def test_cli_run_reports_config_error_on_missing_files(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["run", "-c", str(tmp_path / "missing")])
    assert result.exit_code == 1
    assert "config error" in result.stdout


def test_cli_version() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "taxonomaid" in result.stdout
