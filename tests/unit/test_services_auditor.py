"""Unit tests for the directory auditor."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from taxonomaid.config import WatchConfig
from taxonomaid.services import Auditor, FindingKind

pytestmark = pytest.mark.unit


def _populate(directory: Path, names: list[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_bytes(b"x")


def test_year_drift_detected(tmp_path: Path) -> None:
    target = tmp_path / "Finance" / "Taxes" / "2025"
    _populate(target, ["tax_2025.pdf", "tax_2024.pdf", "tax_2023.pdf", "tax_2022.pdf"])

    findings = Auditor(min_files=2).audit([tmp_path])
    kinds = [f.kind for f in findings]
    assert FindingKind.YEAR_DRIFT in kinds


def test_year_match_passes(tmp_path: Path) -> None:
    target = tmp_path / "Finance" / "Taxes" / "2025"
    _populate(target, ["tax_2025_a.pdf", "tax_2025_b.pdf", "form_2025.pdf", "irs_2025.pdf"])

    findings = Auditor(min_files=2).audit([tmp_path])
    year_findings = [f for f in findings if f.kind is FindingKind.YEAR_DRIFT]
    assert year_findings == []


def test_category_drift_detected(tmp_path: Path) -> None:
    target = tmp_path / "Mixed"
    _populate(
        target,
        ["alpha_one.pdf", "beta_two.pdf", "gamma_three.pdf", "delta_four.pdf"],
    )

    findings = Auditor(min_files=2).audit([tmp_path])
    kinds = [f.kind for f in findings]
    assert FindingKind.CATEGORY_DRIFT in kinds


def test_coherent_directory_no_findings(tmp_path: Path) -> None:
    target = tmp_path / "Receipts"
    _populate(target, ["receipt_1.pdf", "receipt_2.pdf", "receipt_3.pdf", "receipt_4.pdf"])

    findings = Auditor(min_files=2).audit([tmp_path])
    assert findings == ()


def test_min_files_skips_small_directories(tmp_path: Path) -> None:
    target = tmp_path / "Small"
    _populate(target, ["a.pdf"])
    findings = Auditor(min_files=2).audit([tmp_path])
    assert findings == ()


def test_audit_skips_non_directory_root(tmp_path: Path) -> None:
    bogus = tmp_path / "missing"
    findings = Auditor().audit([bogus])
    assert findings == ()


# ---- Unsorted backlog finding (#2 set-and-forget posture) -----------


def test_unsorted_backlog_finding_when_threshold_met(tmp_path: Path) -> None:
    """5+ files older than 7 days in ``_unsorted/`` triggers a finding."""

    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    unsorted = watch_root / "_unsorted"
    unsorted.mkdir()

    # 6 files, all aged 10 days.
    ten_days_ago = time.time() - 10 * 24 * 3600
    for i in range(6):
        f = unsorted / f"old_{i}.pdf"
        f.write_bytes(b"x")
        os.utime(f, (ten_days_ago, ten_days_ago))

    watches = (
        WatchConfig(
            path=watch_root,
            destination_root=watch_root,
        ),
    )
    findings = Auditor().audit([], watches=watches)

    backlog = [f for f in findings if f.kind is FindingKind.UNSORTED_BACKLOG]
    assert len(backlog) == 1
    assert backlog[0].sample_count == 6
    assert "10 day" in backlog[0].details


def test_unsorted_backlog_excludes_recent_files(tmp_path: Path) -> None:
    """Recent files (< threshold age) don't count toward the backlog."""

    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    unsorted = watch_root / "_unsorted"
    unsorted.mkdir()

    # Only 2 old files (default min: 5); should NOT trigger.
    ten_days_ago = time.time() - 10 * 24 * 3600
    for i in range(2):
        f = unsorted / f"old_{i}.pdf"
        f.write_bytes(b"x")
        os.utime(f, (ten_days_ago, ten_days_ago))
    # 10 brand-new files (newer than threshold).
    for i in range(10):
        (unsorted / f"new_{i}.pdf").write_bytes(b"x")

    watches = (
        WatchConfig(
            path=watch_root,
            destination_root=watch_root,
        ),
    )
    findings = Auditor().audit([], watches=watches)

    backlog = [f for f in findings if f.kind is FindingKind.UNSORTED_BACKLOG]
    assert backlog == []  # 2 old files is below the default threshold of 5


def test_unsorted_backlog_check_disabled_when_threshold_zero(tmp_path: Path) -> None:
    """``unsorted_min_files=0`` disables the check entirely."""

    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    unsorted = watch_root / "_unsorted"
    unsorted.mkdir()
    ten_days_ago = time.time() - 10 * 24 * 3600
    for i in range(20):
        f = unsorted / f"old_{i}.pdf"
        f.write_bytes(b"x")
        os.utime(f, (ten_days_ago, ten_days_ago))

    watches = (
        WatchConfig(
            path=watch_root,
            destination_root=watch_root,
        ),
    )
    findings = Auditor(unsorted_min_files=0).audit([], watches=watches)

    backlog = [f for f in findings if f.kind is FindingKind.UNSORTED_BACKLOG]
    assert backlog == []


def test_audit_without_watches_keeps_back_compat(tmp_path: Path) -> None:
    """The auditor's existing single-arg ``audit(roots)`` still works."""
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    # No drift, no unsorted check (no watches passed) -> empty findings.
    assert Auditor().audit([watch_root]) == ()
