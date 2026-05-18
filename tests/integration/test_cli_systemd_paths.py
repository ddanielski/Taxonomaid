"""End-to-end CLI tests for ``taxonomaid systemd-paths``."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from taxonomaid.cli import app

pytestmark = pytest.mark.integration


def _setup_configs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Build a minimal valid config tree under ``tmp_path``.

    Returns ``(config_dir, watch_dir)``.
    """
    monkeypatch.setenv("LLM_API_KEY", "x")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    (config_dir / "watches.yaml").write_text(
        f"""
watches:
  - path: {watch_dir}
    destination_root: {dest_dir}
""",
        encoding="utf-8",
    )
    (config_dir / "llm.yaml").write_text("api_key: ${LLM_API_KEY}\n", encoding="utf-8")
    (config_dir / "notifier.yaml").write_text("apprise_urls: []\n", encoding="utf-8")
    return config_dir, watch_dir


def test_systemd_paths_prints_service_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Print mode emits a ``[Service]`` block ready to drop into a unit."""
    config_dir, watch_dir = _setup_configs(tmp_path, monkeypatch)
    data_dir = tmp_path / "data"

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["systemd-paths", "-c", str(config_dir), "-d", str(data_dir)],
    )

    assert result.exit_code == 0, result.output
    # The block must include the ``[Service]`` section header so the
    # output can be pasted unchanged into a drop-in file.
    assert "[Service]" in result.output
    assert "ReadWritePaths=" in result.output
    # Every relevant root appears.
    for needed in (config_dir.resolve(), data_dir.resolve(), watch_dir.resolve()):
        assert str(needed) in result.output


def test_systemd_paths_writes_dropin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--write-dropin`` writes the block to the given path and reports back."""
    config_dir, watch_dir = _setup_configs(tmp_path, monkeypatch)
    data_dir = tmp_path / "data"
    dropin_path = tmp_path / "dropin" / "readwritepaths.conf"

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "systemd-paths",
            "-c",
            str(config_dir),
            "-d",
            str(data_dir),
            "--write-dropin",
            "--dropin-path",
            str(dropin_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert dropin_path.exists()
    body = dropin_path.read_text(encoding="utf-8")
    # Drop-in must be a complete, parseable systemd fragment.
    assert body.startswith("# Generated")
    assert "[Service]" in body
    assert "ReadWritePaths=" in body
    for needed in (config_dir.resolve(), data_dir.resolve(), watch_dir.resolve()):
        assert str(needed) in body
    # CLI surfaces the next-step reload command.
    assert "daemon-reload" in result.output


def test_systemd_paths_dropin_creates_parent_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drop-in directory is created idempotently if missing."""
    config_dir, _ = _setup_configs(tmp_path, monkeypatch)
    data_dir = tmp_path / "data"
    # Three levels of missing parent dirs.
    dropin_path = tmp_path / "a" / "b" / "c" / "readwritepaths.conf"

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "systemd-paths",
            "-c",
            str(config_dir),
            "-d",
            str(data_dir),
            "--write-dropin",
            "--dropin-path",
            str(dropin_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert dropin_path.exists()


def test_systemd_paths_dropin_overwrites_previous_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running ``--write-dropin`` produces a fresh file (not an append).

    A second invocation after a watch is removed must drop that watch's
    paths from the file, not leave them as stale grants.
    """
    config_dir, watch_dir = _setup_configs(tmp_path, monkeypatch)
    data_dir = tmp_path / "data"
    dropin_path = tmp_path / "readwritepaths.conf"

    runner = CliRunner()
    runner.invoke(
        app,
        [
            "systemd-paths",
            "-c",
            str(config_dir),
            "-d",
            str(data_dir),
            "--write-dropin",
            "--dropin-path",
            str(dropin_path),
        ],
    )
    first = dropin_path.read_text(encoding="utf-8")
    assert str(watch_dir.resolve()) in first

    # Rewrite watches.yaml with a different watch root and re-run.
    new_watch = tmp_path / "different"
    new_watch.mkdir()
    (config_dir / "watches.yaml").write_text(
        f"""
watches:
  - path: {new_watch}
    destination_root: {new_watch}
""",
        encoding="utf-8",
    )
    runner.invoke(
        app,
        [
            "systemd-paths",
            "-c",
            str(config_dir),
            "-d",
            str(data_dir),
            "--write-dropin",
            "--dropin-path",
            str(dropin_path),
        ],
    )
    second = dropin_path.read_text(encoding="utf-8")

    assert str(new_watch.resolve()) in second
    # The old watch root must be gone - drop-ins are accumulators in
    # systemd's view, but on disk we want a single clean source of
    # truth so re-running picks up edits cleanly.
    assert str(watch_dir.resolve()) not in second
