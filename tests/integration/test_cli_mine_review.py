"""End-to-end CLI tests for ``taxonomaid mine`` and ``taxonomaid review``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from taxonomaid.cli import app
from taxonomaid.config import load_rules_file

pytestmark = pytest.mark.integration


def _seed_decisions(data_dir: Path) -> None:
    log = data_dir / "decisions.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, object]] = []
    for i in range(15):
        entries.append(
            {
                "decision_id": f"d{i}",
                "ts": "2026-05-17T12:00:00+00:00",
                "file": f"receipt_{i}.pdf",
                "destination": "Receipts",
                "source": "llm",
                "confidence": 0.92,
                "rule_id": None,
                "reason": "looks like a receipt",
                "features": {},
            }
        )
    with log.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def test_mine_writes_proposals(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    data_dir = tmp_path / "data"
    _seed_decisions(data_dir)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "mine",
            "-c",
            str(config_dir),
            "-d",
            str(data_dir),
            "--min-samples",
            "5",
            "--agreement",
            "0.9",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "mined" in result.stdout

    proposals = load_rules_file(config_dir / "proposed_rules.yaml")
    assert len(proposals) == 1
    assert "receipt" in proposals[0].id


def test_mine_no_log(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["mine", "-c", str(config_dir), "-d", str(tmp_path / "data")],
    )
    assert result.exit_code == 0
    assert "no decision log" in result.stdout


def test_review_approves_writes_to_rules(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    data_dir = tmp_path / "data"
    _seed_decisions(data_dir)

    runner = CliRunner()
    runner.invoke(
        app,
        ["mine", "-c", str(config_dir), "-d", str(data_dir), "--min-samples", "5"],
    )

    result = runner.invoke(app, ["review", "-c", str(config_dir)], input="a\n")
    assert result.exit_code == 0
    rules = load_rules_file(config_dir / "rules.yaml")
    assert len(rules) == 1
    assert load_rules_file(config_dir / "proposed_rules.yaml") == ()


def test_review_rejects_writes_to_rejected(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    data_dir = tmp_path / "data"
    _seed_decisions(data_dir)

    runner = CliRunner()
    runner.invoke(
        app,
        ["mine", "-c", str(config_dir), "-d", str(data_dir), "--min-samples", "5"],
    )

    result = runner.invoke(app, ["review", "-c", str(config_dir)], input="r\n")
    assert result.exit_code == 0
    rejected = load_rules_file(config_dir / "rejected_rules.yaml")
    assert len(rejected) == 1


def test_review_skips_keeps_in_proposals(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    data_dir = tmp_path / "data"
    _seed_decisions(data_dir)

    runner = CliRunner()
    runner.invoke(
        app,
        ["mine", "-c", str(config_dir), "-d", str(data_dir), "--min-samples", "5"],
    )

    result = runner.invoke(app, ["review", "-c", str(config_dir)], input="s\n")
    assert result.exit_code == 0
    proposals = load_rules_file(config_dir / "proposed_rules.yaml")
    assert len(proposals) == 1


def test_review_when_no_proposals(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(app, ["review", "-c", str(config_dir)])
    assert result.exit_code == 0
    assert "No proposals" in result.stdout


def test_mine_does_not_re_propose_rejected(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    data_dir = tmp_path / "data"
    _seed_decisions(data_dir)

    runner = CliRunner()
    runner.invoke(
        app,
        ["mine", "-c", str(config_dir), "-d", str(data_dir), "--min-samples", "5"],
    )
    runner.invoke(app, ["review", "-c", str(config_dir)], input="r\n")

    result = runner.invoke(
        app,
        ["mine", "-c", str(config_dir), "-d", str(data_dir), "--min-samples", "5"],
    )
    assert result.exit_code == 0
    proposals = load_rules_file(config_dir / "proposed_rules.yaml")
    assert proposals == ()
