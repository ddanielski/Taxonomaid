"""Unit tests for :mod:`taxonomaid.services.similarity`."""

from __future__ import annotations

from pathlib import Path

import pytest

from taxonomaid.domain import DecisionSource
from taxonomaid.services import SimilarityIndex
from taxonomaid.services.dispatcher import _SIMILARITY_INDEXED_SOURCES

pytestmark = pytest.mark.unit


def test_top_matches_returns_similar_filenames() -> None:
    idx = SimilarityIndex()
    idx.add(filename="tax_2023.pdf", destination=Path("Finance/Taxes/2023"))
    idx.add(filename="tax_2024.pdf", destination=Path("Finance/Taxes/2024"))
    idx.add(filename="invoice_3.pdf", destination=Path("Finance/Invoices"))

    matches = idx.top_matches("tax_2025.pdf")
    files = [m[0] for m in matches]
    assert "tax_2024.pdf" in files
    assert "invoice_3.pdf" not in files


def test_top_matches_respects_limit() -> None:
    idx = SimilarityIndex()
    for i in range(10):
        idx.add(filename=f"report_{i}.docx", destination=Path("Reports"))
    matches = idx.top_matches("report_999.docx", limit=3)
    assert len(matches) <= 3


def test_top_matches_empty_for_no_overlap() -> None:
    idx = SimilarityIndex()
    idx.add(filename="alpha.pdf", destination=Path("A"))
    assert idx.top_matches("zeta.docx") == ()


def test_top_matches_skips_below_min_score() -> None:
    idx = SimilarityIndex()
    idx.add(filename="report_alpha_beta_gamma.pdf", destination=Path("Reports"))
    matches = idx.top_matches("report.pdf", min_score=0.9)
    assert matches == ()


def test_add_many() -> None:
    idx = SimilarityIndex()
    idx.add_many(
        [
            ("invoice_2024.pdf", Path("Finance/Invoices")),
            ("invoice_2025.pdf", Path("Finance/Invoices")),
        ]
    )
    matches = idx.top_matches("invoice_new.pdf")
    assert len(matches) == 2


def test_add_deduplicates_identical_pair() -> None:
    """L-new-5 regression: re-adding the same (filename, destination) is a no-op.

    A crash-recovery replay could re-emit a placement; without
    deduplication that doubles the sample's weight in the top-K
    scoring, biasing future LLM prompts toward the duplicated
    destination.
    """
    idx = SimilarityIndex()
    idx.add(filename="report_q1.pdf", destination=Path("Reports/Q1"))
    idx.add(filename="report_q1.pdf", destination=Path("Reports/Q1"))
    idx.add(filename="report_q1.pdf", destination=Path("Reports/Q1"))
    assert len(idx.samples) == 1


def test_add_keeps_distinct_destinations_separate() -> None:
    """Same filename, different destinations: both entries are retained."""
    idx = SimilarityIndex()
    idx.add(filename="report.pdf", destination=Path("Reports/Q1"))
    idx.add(filename="report.pdf", destination=Path("Reports/Q2"))
    assert len(idx.samples) == 2


def test_user_override_decisions_are_not_indexed_for_similarity() -> None:
    """Test R regression: ``USER_OVERRIDE`` is intentionally excluded.

    The dispatcher's ``_index_for_similarity`` filters by
    ``_SIMILARITY_INDEXED_SOURCES`` which lists ``RULE``, ``LLM``,
    and ``NOTIFIER_CONFIRMED``. A user manually moving a file
    after auto-placement (``USER_OVERRIDE``) is a *negative*
    signal about the prior placement, not a positive sample of
    "files like this go here"; indexing it would teach the LLM
    the wrong taxonomy. Pin the membership so a refactor that
    quietly adds ``USER_OVERRIDE`` to the indexed set is caught.
    """
    assert DecisionSource.USER_OVERRIDE not in _SIMILARITY_INDEXED_SOURCES
    assert DecisionSource.RULE in _SIMILARITY_INDEXED_SOURCES
    assert DecisionSource.LLM in _SIMILARITY_INDEXED_SOURCES
    assert DecisionSource.NOTIFIER_CONFIRMED in _SIMILARITY_INDEXED_SOURCES


def test_runtime_cap_evicts_oldest() -> None:
    """Test E (companion): runtime additions past ``max_samples`` evict FIFO.

    The warm-up cap is exercised at the dispatcher level; this
    test pins the eviction itself on the index. Past the cap,
    the *oldest* sample is dropped - newest samples carry the
    highest signal for current user taste.
    """
    idx = SimilarityIndex(max_samples=3)
    idx.add(filename="oldest.pdf", destination=Path("A"))
    idx.add(filename="middle.pdf", destination=Path("B"))
    idx.add(filename="newest_a.pdf", destination=Path("C"))
    assert len(idx.samples) == 3
    # One more triggers eviction of ``oldest``.
    idx.add(filename="newest_b.pdf", destination=Path("D"))
    assert len(idx.samples) == 3
    filenames = {s.filename for s in idx.samples}
    assert "oldest.pdf" not in filenames
    assert "newest_b.pdf" in filenames
