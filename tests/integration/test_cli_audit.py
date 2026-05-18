"""End-to-end CLI tests for ``taxonomaid audit``."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from taxonomaid.cli import app

pytestmark = pytest.mark.integration


def _setup_configs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LLM_API_KEY", "x")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()

    (config_dir / "watches.yaml").write_text(
        f"""
watches:
  - path: {watch_dir}
    destination_root: {watch_dir}
""",
        encoding="utf-8",
    )
    (config_dir / "llm.yaml").write_text("api_key: ${LLM_API_KEY}\n", encoding="utf-8")
    (config_dir / "notifier.yaml").write_text("apprise_urls: []\n", encoding="utf-8")
    return watch_dir


def test_audit_no_findings_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    watch_dir = _setup_configs(tmp_path, monkeypatch)
    target = watch_dir / "Receipts"
    target.mkdir()
    for i in range(4):
        (target / f"receipt_{i}.pdf").write_bytes(b"x")

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "audit",
            "-c",
            str(tmp_path / "config"),
            "-d",
            str(tmp_path / "data"),
            "--min-files",
            "2",
        ],
    )
    assert result.exit_code == 0
    assert "no coherence findings" in result.stdout


def test_audit_year_drift_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    watch_dir = _setup_configs(tmp_path, monkeypatch)
    target = watch_dir / "Finance" / "Taxes" / "2025"
    target.mkdir(parents=True)
    for i in range(4):
        (target / f"tax_2024_{i}.pdf").write_bytes(b"x")

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "audit",
            "-c",
            str(tmp_path / "config"),
            "-d",
            str(tmp_path / "data"),
            "--min-files",
            "2",
        ],
    )
    assert result.exit_code == 1
    assert "year_drift" in result.stdout
