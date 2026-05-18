"""Unit tests for :mod:`taxonomaid.adapters.filesystem.local`."""

from __future__ import annotations

from pathlib import Path

import pytest

from taxonomaid.adapters.filesystem import LocalFilesystem
from taxonomaid.domain import FileSystemError

pytestmark = pytest.mark.unit


def test_move_creates_parent_dirs(tmp_path: Path) -> None:
    fs = LocalFilesystem()
    src = tmp_path / "src.txt"
    src.write_text("hi", encoding="utf-8")
    dst = tmp_path / "deeply" / "nested" / "dst.txt"

    fs.move(src, dst)

    assert dst.exists()
    assert not src.exists()
    assert dst.read_text(encoding="utf-8") == "hi"


def test_size_reports_byte_length(tmp_path: Path) -> None:
    fs = LocalFilesystem()
    f = tmp_path / "f.bin"
    f.write_bytes(b"abcde")
    assert fs.size(f) == 5


def test_mkdir_idempotent(tmp_path: Path) -> None:
    fs = LocalFilesystem()
    fs.mkdir(tmp_path / "d")
    fs.mkdir(tmp_path / "d")


def test_size_missing_raises_filesystem_error(tmp_path: Path) -> None:
    fs = LocalFilesystem()
    with pytest.raises(FileSystemError):
        fs.size(tmp_path / "absent")


def test_exists_and_is_file(tmp_path: Path) -> None:
    fs = LocalFilesystem()
    f = tmp_path / "f.txt"
    f.write_text("x", encoding="utf-8")
    assert fs.exists(f)
    assert fs.is_file(f)
    assert not fs.is_file(tmp_path)
