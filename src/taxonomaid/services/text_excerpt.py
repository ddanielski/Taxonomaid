"""File-content excerpt helpers.

Dispatches by extension to specialised extractors for PDF, DOCX, and RTF;
falls back to a UTF-8 read for everything else. Every extractor degrades
gracefully to an empty string when the underlying library fails so a
malformed file never crashes the dispatcher - the LLM is simply asked
to classify on filename alone in that case.

The functions are intentionally synchronous; the dispatcher wraps the
call in :func:`asyncio.to_thread` to keep the event loop responsive on
slow disks.

Security posture
----------------

The extractors call third-party libraries (``pypdf``,
``python-docx``, ``striprtf``) on attacker-controlled bytes. Caveats
the operator should know:

- ``pypdf`` is pure Python and has had a steady cadence of CVEs
  (resource exhaustion, parser confusion). A poppler-binding
  alternative (``pdftotext`` in a subprocess) would give a
  battle-hardened surface and real memory isolation; we prefer the
  pure-Python option today for portability, and the swap is local
  if the daemon ever runs against untrusted documents.
- The dispatcher caps **input size**
  (``_MAX_INPUT_BYTES_FOR_EXTRACTION``) and **wall-clock time**
  (``_EXTRACTION_TIMEOUT_S``) but does **not** bound RAM: a
  pathological PDF that decompresses to gigabytes inside ``pypdf``
  will OOM before the timeout fires.
- ``asyncio.wait_for(asyncio.to_thread(...))`` cancels the
  *caller* on timeout; it does not terminate the underlying worker
  thread. A determined attacker who can keep dropping pathological
  inputs could exhaust the default thread-pool over time.

The recommended Phase-7 fix is to run extraction in a subprocess
with ``resource.setrlimit`` (RLIMIT_AS / RLIMIT_CPU) so the kernel
enforces the budget. For a single-user personal NAS feeding
documents the operator already trusts, the current posture is
fine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pypdf.errors
import structlog
from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from pypdf import PdfReader
from striprtf.striprtf import rtf_to_text

_log = structlog.get_logger("taxonomaid.text_excerpt")

_PDF_SUFFIX: Final[str] = ".pdf"
_DOCX_SUFFIX: Final[str] = ".docx"
_RTF_SUFFIX: Final[str] = ".rtf"

# How much over-fetch to allow before decoding plain-text files. UTF-8
# code points are at most 4 bytes wide, so 4 extra bytes guarantee we
# don't truncate a multi-byte character at the buffer boundary.
_UTF8_OVERREAD_BYTES: Final[int] = 4

# The set of "extractor failed" exceptions we expect to recover from.
# Anything outside this tuple - SystemExit, KeyboardInterrupt,
# MemoryError - propagates so we don't accidentally swallow fatal
# conditions while trying to be defensive.
_PDF_EXTRACT_ERRORS: Final[tuple[type[BaseException], ...]] = (
    OSError,
    ValueError,
    KeyError,
    AttributeError,
    pypdf.errors.PyPdfError,
)
_DOCX_EXTRACT_ERRORS: Final[tuple[type[BaseException], ...]] = (
    OSError,
    ValueError,
    KeyError,
    AttributeError,
    PackageNotFoundError,
)
_RTF_EXTRACT_ERRORS: Final[tuple[type[BaseException], ...]] = (
    OSError,
    ValueError,
    KeyError,
    AttributeError,
    UnicodeDecodeError,
)


def read_excerpt(path: Path, *, max_chars: int) -> str:
    """Return up to ``max_chars`` characters of extracted text from ``path``.

    Args:
        path: File to read.
        max_chars: Hard cap on the **character** length of the returned
            string. Every extractor truncates after decoding so the cap
            never splits a multi-byte UTF-8 codepoint.

    Returns:
        Decoded text. Empty string if the file is missing, an extractor
        raises, or the format produces no text. Bytes that fail to
        decode are replaced with the Unicode replacement character.
    """
    suffix = path.suffix.lower()
    if suffix == _PDF_SUFFIX:
        return _read_pdf(path, max_chars=max_chars)
    if suffix == _DOCX_SUFFIX:
        return _read_docx(path, max_chars=max_chars)
    if suffix == _RTF_SUFFIX:
        return _read_rtf(path, max_chars=max_chars)
    return _read_text(path, max_chars=max_chars)


def _read_text(path: Path, *, max_chars: int) -> str:
    # Over-read by a few bytes so the UTF-8 boundary at max_chars is
    # safe to truncate after decode without splitting a codepoint.
    over_read = max_chars * 4 + _UTF8_OVERREAD_BYTES
    try:
        with path.open("rb") as fh:
            raw = fh.read(over_read)
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")[:max_chars]


def _read_pdf(path: Path, *, max_chars: int) -> str:
    try:
        reader = PdfReader(str(path))
        pieces: list[str] = []
        total = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            if not text:
                continue
            pieces.append(text)
            total += len(text)
            if total >= max_chars:
                break
        return "\n".join(pieces)[:max_chars]
    except _PDF_EXTRACT_ERRORS as exc:
        _log.warning("extract_failed", format="pdf", path=str(path), error=str(exc))
        return ""


def _read_docx(path: Path, *, max_chars: int) -> str:
    try:
        doc = Document(str(path))
        pieces: list[str] = []
        total = 0
        for para in doc.paragraphs:
            text = para.text
            if not text:
                continue
            pieces.append(text)
            total += len(text)
            if total >= max_chars:
                break
        return "\n".join(pieces)[:max_chars]
    except _DOCX_EXTRACT_ERRORS as exc:
        _log.warning("extract_failed", format="docx", path=str(path), error=str(exc))
        return ""


def _read_rtf(path: Path, *, max_chars: int) -> str:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        plain: str = rtf_to_text(raw)  # type: ignore[no-untyped-call]
        return plain[:max_chars]
    except _RTF_EXTRACT_ERRORS as exc:
        _log.warning("extract_failed", format="rtf", path=str(path), error=str(exc))
        return ""
