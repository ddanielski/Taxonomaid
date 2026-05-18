"""Unit tests for :mod:`taxonomaid.services.text_excerpt`."""

from __future__ import annotations

from pathlib import Path

import pytest
from docx import Document
from pypdf import PdfWriter

from taxonomaid.services import text_excerpt
from taxonomaid.services.text_excerpt import read_excerpt

pytestmark = pytest.mark.unit


def test_read_excerpt_caps_at_max_chars(tmp_path: Path) -> None:
    body = b"abcdefghijklmnopqrstuvwxyz"
    f = tmp_path / "f.txt"
    f.write_bytes(body)
    assert read_excerpt(f, max_chars=5) == "abcde"


def test_read_excerpt_decodes_with_replacement(tmp_path: Path) -> None:
    f = tmp_path / "f.bin"
    f.write_bytes(b"\xff\xfe\xfd")
    out = read_excerpt(f, max_chars=10)
    assert "\ufffd" in out


def test_read_excerpt_missing_returns_empty(tmp_path: Path) -> None:
    assert read_excerpt(tmp_path / "absent", max_chars=10) == ""


@pytest.mark.parametrize(
    "suffix, dispatch_target",
    [
        (".pdf", "_read_pdf"),
        (".docx", "_read_docx"),
        (".rtf", "_read_rtf"),
        (".txt", "_read_text"),
        (".unknown", "_read_text"),
    ],
)
def test_dispatch_routes_by_extension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    dispatch_target: str,
) -> None:
    captured: list[str] = []

    def fake(path: Path, *, max_chars: int) -> str:
        captured.append(dispatch_target)
        return f"MOCK_{dispatch_target}"

    monkeypatch.setattr(text_excerpt, dispatch_target, fake)
    out = read_excerpt(tmp_path / f"file{suffix}", max_chars=100)
    assert captured == [dispatch_target]
    assert out == f"MOCK_{dispatch_target}"


def test_pdf_extraction_returns_empty_on_corrupt_file(tmp_path: Path) -> None:
    f = tmp_path / "broken.pdf"
    f.write_bytes(b"definitely not a pdf")
    assert read_excerpt(f, max_chars=100) == ""


def test_docx_extraction_returns_empty_on_corrupt_file(tmp_path: Path) -> None:
    f = tmp_path / "broken.docx"
    f.write_bytes(b"not a docx either")
    assert read_excerpt(f, max_chars=100) == ""


def test_rtf_extraction_returns_text(tmp_path: Path) -> None:
    f = tmp_path / "note.rtf"
    f.write_text(r"{\rtf1\ansi Hello Taxonomaid.}", encoding="utf-8")
    out = read_excerpt(f, max_chars=100)
    assert "Hello" in out
    assert "Taxonomaid" in out


def test_pdf_extraction_returns_some_text(tmp_path: Path) -> None:
    pdf = _build_blank_pdf(tmp_path / "blank.pdf")
    # Blank pages produce empty text but the call must not raise.
    assert read_excerpt(pdf, max_chars=100) == ""


def test_docx_extraction_returns_paragraph_text(tmp_path: Path) -> None:
    docx_path = tmp_path / "note.docx"
    document = Document()
    document.add_paragraph("Electrical Engineering Exam - December 2013")
    document.add_paragraph("Question 1: explain Ohm's law.")
    document.save(str(docx_path))

    out = read_excerpt(docx_path, max_chars=200)
    assert "Electrical Engineering" in out
    assert "Ohm" in out


def _build_blank_pdf(target: Path) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with target.open("wb") as fh:
        writer.write(fh)
    return target
