"""Unit tests for :mod:`taxonomaid.services.path_safety`."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from taxonomaid.domain import FileSystemError
from taxonomaid.services.path_safety import (
    collision_free_path,
    safe_resolve,
    safe_unsorted_dir,
)

pytestmark = pytest.mark.unit


def test_safe_resolve_relative_path(tmp_path: Path) -> None:
    root = tmp_path / "watch"
    root.mkdir()
    target = safe_resolve(root, Path("CVs"))
    assert target == (root / "CVs").resolve()


def test_safe_resolve_strips_redundant_watch_root_prefix(tmp_path: Path) -> None:
    root = tmp_path / "test-watch"
    root.mkdir()
    target = safe_resolve(root, Path("test-watch/CVs"))
    assert target == (root / "CVs").resolve()


def test_safe_resolve_strips_prefix_with_nested_path(tmp_path: Path) -> None:
    root = tmp_path / "watch"
    root.mkdir()
    target = safe_resolve(root, Path("watch/Career/CVs"))
    assert target == (root / "Career" / "CVs").resolve()


def test_safe_resolve_makes_absolute_relative(tmp_path: Path) -> None:
    root = tmp_path / "watch"
    root.mkdir()
    target = safe_resolve(root, Path("/Reports/Q1"))
    assert target == (root / "Reports" / "Q1").resolve()


def test_safe_resolve_rejects_dotdot_traversal(tmp_path: Path) -> None:
    root = tmp_path / "watch"
    root.mkdir()
    assert safe_resolve(root, Path("../../etc/passwd")) is None


def test_safe_resolve_rejects_inner_traversal(tmp_path: Path) -> None:
    root = tmp_path / "watch"
    root.mkdir()
    assert safe_resolve(root, Path("foo/../../escape")) is None


def test_safe_resolve_root_equivalent_destinations_are_parked(tmp_path: Path) -> None:
    """A destination that collapses to the watch root itself is now refused.

    Previously, an LLM returning ``""``, ``"."``, ``"/"``, or the
    watch-root basename would silently land the file at the watch
    root. That's auto-placement without any meaningful classification;
    we refuse and let the caller park instead.
    """
    root = tmp_path / "watch"
    root.mkdir()
    assert safe_resolve(root, Path("watch")) is None
    assert safe_resolve(root, Path()) is None
    assert safe_resolve(root, Path("/")) is None


def test_safe_resolve_basename_followed_by_dotdot_is_rejected(tmp_path: Path) -> None:
    """Sev-3 regression: ``watch/../../escape`` doesn't slip past the strip.

    The basename strip is one-shot and happens BEFORE resolution. So
    ``watch/../../escape`` becomes ``../../escape`` (the leading
    ``watch`` is stripped), then resolves outside the watch root,
    and the ``relative_to`` containment check returns ``None``. The
    end behaviour is correct; this test pins the interaction so a
    future refactor (e.g. moving the strip after resolution)
    doesn't silently weaken the guard.
    """
    root = tmp_path / "watch"
    root.mkdir()
    assert safe_resolve(root, Path("watch/../../escape")) is None
    assert safe_resolve(root, Path("watch/../../../etc/passwd")) is None
    # Single-shot strip: ``watch/watch/..`` becomes ``watch/..``
    # after the strip and the ``..`` cancels the remaining ``watch``,
    # producing a root-equivalent destination - which the new shape
    # check rejects rather than silently placing at the watch root.
    assert safe_resolve(root, Path("watch/watch/..")) is None


def test_safe_resolve_rejects_nul_byte(tmp_path: Path) -> None:
    """NUL bytes should fail early, not deep inside ``Path.resolve``."""
    root = tmp_path / "watch"
    root.mkdir()
    assert safe_resolve(root, Path("Reports\x00/Q1")) is None


def test_safe_resolve_rejects_control_chars(tmp_path: Path) -> None:
    """Control characters in a path are a prompt-injection escape vector."""
    root = tmp_path / "watch"
    root.mkdir()
    assert safe_resolve(root, Path("Reports\n/Q1")) is None
    assert safe_resolve(root, Path("Reports\r/Q1")) is None
    assert safe_resolve(root, Path("Reports\x1b[31m/Q1")) is None


def test_safe_resolve_rejects_pathologically_deep_paths(tmp_path: Path) -> None:
    """A 9-segment path is rejected; an 8-segment one is allowed."""
    root = tmp_path / "watch"
    root.mkdir()
    deep_eight = Path("a/b/c/d/e/f/g/h")
    deep_nine = Path("a/b/c/d/e/f/g/h/i")
    assert safe_resolve(root, deep_eight) is not None
    assert safe_resolve(root, deep_nine) is None


def test_collision_free_returns_target_when_unused(tmp_path: Path) -> None:
    target = tmp_path / "foo.txt"
    out = collision_free_path(target, exists=lambda _p: False)
    assert out == target


def test_collision_free_appends_counter(tmp_path: Path) -> None:
    target = tmp_path / "foo.txt"
    target.write_text("x", encoding="utf-8")
    out = collision_free_path(target, exists=Path.exists)
    assert out == tmp_path / "foo (2).txt"


def test_collision_free_skips_already_taken_counters(tmp_path: Path) -> None:
    (tmp_path / "foo.txt").write_text("a", encoding="utf-8")
    (tmp_path / "foo (2).txt").write_text("b", encoding="utf-8")
    (tmp_path / "foo (3).txt").write_text("c", encoding="utf-8")
    out = collision_free_path(tmp_path / "foo.txt", exists=Path.exists)
    assert out == tmp_path / "foo (4).txt"


def test_collision_free_preserves_extension(tmp_path: Path) -> None:
    target = tmp_path / "report.tar.gz"
    target.write_text("x", encoding="utf-8")
    out = collision_free_path(target, exists=Path.exists)
    assert out == tmp_path / "report.tar (2).gz"


# ---- Property-based tests (Test I) ----------------------------------
#
# Encode the security-adjacent helpers' contracts as properties so a
# regression in the strip/resolve interaction can't slip past unit
# tests written against fixed inputs.


from hypothesis import given  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

# Each segment intentionally avoids ``/``, ``\0``, and ``.`` so we
# don't trip over OS-specific path semantics; ``..`` traversal is
# exercised by dedicated unit tests above.
_SAFE_SEGMENT = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N"),  # letters + numbers
        min_codepoint=0x20,
        max_codepoint=0x7E,
    ),
    min_size=1,
    max_size=12,
)
_RELATIVE_PATH = st.lists(_SAFE_SEGMENT, min_size=1, max_size=5).map(lambda parts: Path(*parts))


@given(relative=_RELATIVE_PATH)
def test_property_safe_resolve_result_is_contained_when_not_none(
    relative: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """For any safe-segment relative path: result is either ``None`` or under root."""
    root = tmp_path_factory.mktemp("watch")
    result = safe_resolve(root, relative)
    if result is None:
        return
    assert result.is_relative_to(root.resolve())


@given(relative=_RELATIVE_PATH)
def test_property_safe_resolve_is_idempotent(
    relative: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Calling ``safe_resolve`` on its own output reproduces the same path.

    If the first call accepts ``relative`` (returns a contained
    path), passing that resolved absolute path back in should
    yield the same contained path - the basename strip + absolute
    handling are idempotent under repeated application.
    """
    root = tmp_path_factory.mktemp("watch")
    first = safe_resolve(root, relative)
    if first is None:
        return
    relative_to_root = first.relative_to(root.resolve())
    second = safe_resolve(root, relative_to_root)
    assert second == first


@given(name=_SAFE_SEGMENT, taken=st.integers(min_value=0, max_value=20))
def test_property_collision_free_returns_unused_sibling(
    name: str, taken: int, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """``collision_free_path`` always returns an unused path in the same parent."""
    parent = tmp_path_factory.mktemp("collisions")
    target = parent / f"{name}.txt"

    # Synthetic "exists" set: the canonical name plus a prefix of
    # ``(n)`` siblings is taken.
    existing: set[Path] = set()
    if taken > 0:
        existing.add(target)
        for n in range(2, taken + 1):
            existing.add(parent / f"{name} ({n}).txt")

    result = collision_free_path(target, exists=existing.__contains__)
    # Property 1: result is not in the "exists" set.
    assert result not in existing
    # Property 2: result lives in the same parent directory.
    assert result.parent == target.parent
    # Property 3: when the target was taken, the result differs from it.
    if target in existing:
        assert result != target


def test_collision_free_raises_filesystem_error_after_max_suffix(tmp_path: Path) -> None:
    """Exhausting collision suffixes raises the port-level error.

    The dispatcher's inbound loop catches :class:`TaxonomaidError`
    (of which :class:`FileSystemError` is a subclass), so the
    inbound task survives a pathological collision-storm rather than
    asymmetrically tearing down. Bare ``FileExistsError`` would
    escape that catch.
    """
    target = tmp_path / "foo.txt"
    with pytest.raises(FileSystemError, match="every collision-suffix slot"):
        # ``exists`` always returns ``True`` so every candidate is
        # taken; we hit the ceiling on the very first scan.
        collision_free_path(target, exists=lambda _p: True)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlinks")
def test_safe_unsorted_dir_rejects_symlink_escape(tmp_path: Path) -> None:
    """C1 regression: a symlinked _unsorted tray is refused.

    Without this guard, a symlink whose target lives outside
    ``destination_root`` would silently redirect parked files. The
    dispatcher catches ``None`` from this helper and refuses to
    park, leaving the file at its source.
    """
    root = tmp_path / "watch"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    tray = root / "_unsorted"
    tray.symlink_to(outside)
    assert safe_unsorted_dir(root, Path("_unsorted")) is None


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlinks")
def test_safe_unsorted_dir_accepts_real_dir(tmp_path: Path) -> None:
    root = tmp_path / "watch"
    root.mkdir()
    (root / "_unsorted").mkdir()
    target = safe_unsorted_dir(root, Path("_unsorted"))
    assert target == (root / "_unsorted").resolve()
