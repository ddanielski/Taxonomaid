"""Unit tests for the depth-bounded candidate-destination walker."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from taxonomaid.domain import FileEvent, FileEventKind
from taxonomaid.services.dispatcher import _candidate_destinations

pytestmark = pytest.mark.unit


def _event(watch_root: Path) -> FileEvent:
    return FileEvent(
        path=watch_root / "f.txt",
        kind=FileEventKind.ADDED,
        watch_root=watch_root,
        destination_root=watch_root,
        unsorted_dir=Path("_unsorted"),
    )


def test_returns_relative_paths(tmp_path: Path) -> None:
    (tmp_path / "Reports").mkdir()
    (tmp_path / "Invoices").mkdir()
    candidates = _candidate_destinations(_event(tmp_path))
    names = {str(c) for c in candidates}
    assert "Reports" in names
    assert "Invoices" in names


def test_walks_nested_taxonomy(tmp_path: Path) -> None:
    (tmp_path / "Finance" / "Taxes" / "2025").mkdir(parents=True)
    (tmp_path / "Finance" / "Receipts").mkdir(parents=True)
    candidates = {str(c) for c in _candidate_destinations(_event(tmp_path))}
    assert "Finance" in candidates
    assert "Finance/Taxes" in candidates
    assert "Finance/Taxes/2025" in candidates
    assert "Finance/Receipts" in candidates


def test_excludes_unsorted_dir(tmp_path: Path) -> None:
    (tmp_path / "_unsorted").mkdir()
    (tmp_path / "Reports").mkdir()
    candidates = {str(c) for c in _candidate_destinations(_event(tmp_path))}
    assert "_unsorted" not in candidates


def test_skips_hidden_directories(tmp_path: Path) -> None:
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / "Reports").mkdir()
    candidates = {str(c) for c in _candidate_destinations(_event(tmp_path))}
    assert ".cache" not in candidates
    assert ".git" not in candidates


def test_returns_empty_when_root_missing(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-root"
    candidates = _candidate_destinations(
        FileEvent(
            path=missing / "f.txt",
            kind=FileEventKind.ADDED,
            watch_root=missing,
            destination_root=missing,
            unsorted_dir=Path("_unsorted"),
        )
    )
    assert candidates == ()


def test_candidate_limit_caps_at_64(tmp_path: Path) -> None:
    """Test D: pin ``_CANDIDATE_LIMIT`` so the prompt-budget defence holds.

    Walking a sibling-heavy taxonomy without a cap would blow up
    the LLM prompt token count and slow every classification.
    The hard cap (currently 64) keeps the prompt bounded; this
    test pins the contract so a future refactor that, say,
    quietly raises the cap is visible in code review.
    """
    # Create 100 sibling directories. Any subset of 64 may be
    # returned (insertion order from ``iterdir`` is OS-dependent),
    # but the count must be exactly the cap.
    for i in range(100):
        (tmp_path / f"folder_{i:03d}").mkdir()
    candidates = _candidate_destinations(_event(tmp_path))
    assert len(candidates) == 64


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlinks")
def test_does_not_recurse_through_symlink_cycle(tmp_path: Path) -> None:
    """H7 regression: in-tree symlink cycles can't bloat the candidate list.

    A symlink cycle (``A -> B`` while ``B/loop -> A``) used to make
    the BFS revisit the same resolved directory once per depth
    level. The visited-set guard collapses it to a single entry.
    """
    a_dir = tmp_path / "A"
    b_dir = tmp_path / "B"
    a_dir.mkdir()
    b_dir.mkdir()
    # A/loop points at B; B/loop points back at A.
    (a_dir / "loop").symlink_to(b_dir, target_is_directory=True)
    (b_dir / "loop").symlink_to(a_dir, target_is_directory=True)

    candidates = _candidate_destinations(_event(tmp_path))
    # Each unique resolved directory should appear at most once;
    # without the visited set, we'd see ``A``, ``B``, ``A/loop``,
    # ``B/loop``, ``A/loop/loop``, ... up to the depth cap.
    assert len(candidates) == len(set(candidates))
