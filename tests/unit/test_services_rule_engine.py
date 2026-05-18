"""Unit tests for the rule engine matcher and scorer."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from taxonomaid.domain import CoherenceSpec, MatchSpec, Rule
from taxonomaid.services import RuleEngine

pytestmark = pytest.mark.unit


def _r(
    rid: str,
    *,
    weight: float = 1.0,
    confidence: float = 1.0,
    match: MatchSpec | None = None,
    destination_template: str = "Misc/",
    coherence: CoherenceSpec | None = None,
) -> Rule:
    return Rule(
        id=rid,
        match=match or MatchSpec(),
        destination_template=destination_template,
        coherence=coherence or CoherenceSpec(),
        weight=weight,
        confidence=confidence,
    )


def test_engine_orders_rules_by_score() -> None:
    a = _r("a", weight=0.5, confidence=0.5)
    b = _r("b", weight=1.0, confidence=1.0)
    c = _r("c", weight=0.9, confidence=0.5)
    engine = RuleEngine(rules=(a, b, c))
    assert [r.id for r in engine.rules] == ["b", "c", "a"]


def test_no_match_when_predicate_fails() -> None:
    engine = RuleEngine(
        rules=(
            _r(
                "ext_only",
                match=MatchSpec(ext=(".pdf",)),
                destination_template="Pdfs/",
            ),
        ),
    )
    result = engine.match(filename="report.txt", ext=".txt", content=None)
    assert not result.matched


def test_extension_match_normalises_case() -> None:
    engine = RuleEngine(
        rules=(
            _r(
                "ext_only",
                match=MatchSpec(ext=(".pdf",)),
                destination_template="Pdfs/",
            ),
        ),
    )
    result = engine.match(filename="report.PDF", ext=".PDF", content=None)
    assert result.matched
    assert result.destination == Path("Pdfs/")


def test_filename_regex_match() -> None:
    engine = RuleEngine(
        rules=(
            _r(
                "tax",
                match=MatchSpec(filename_regex=r"(?i)tax"),
                destination_template="Finance/",
            ),
        ),
    )
    assert engine.match(filename="my_TAX_2025.pdf", ext=".pdf", content=None).matched
    assert not engine.match(filename="other.pdf", ext=".pdf", content=None).matched


def test_content_keywords_match() -> None:
    engine = RuleEngine(
        rules=(
            _r(
                "irs",
                match=MatchSpec(content_keywords=("IRS",)),
                destination_template="Finance/",
            ),
        ),
    )
    assert engine.match(filename="x.pdf", ext=".pdf", content="IRS notice").matched
    assert not engine.match(filename="x.pdf", ext=".pdf", content="other").matched


def test_mime_types_match() -> None:
    """Q regression: ``mime_types`` is honoured (was previously silently dropped)."""
    engine = RuleEngine(
        rules=(
            _r(
                "pdfs",
                match=MatchSpec(mime_types=("application/pdf",)),
                destination_template="Documents/",
            ),
        ),
    )
    # .pdf -> application/pdf via stdlib mimetypes.guess_type.
    assert engine.match(filename="report.pdf", ext=".pdf", content=None).matched
    # .txt -> text/plain; mismatched, no rule fires.
    assert not engine.match(filename="notes.txt", ext=".txt", content=None).matched


def test_redos_prone_regex_demonstrates_trusted_input_boundary() -> None:
    """M (security): pin the "rule corpus is trusted input" contract.

    The rule corpus is documented as trusted input - the config
    layer warns on nested-quantifier patterns but does not reject
    them. This test executes that contract two ways:

    1. A short pathological input (``a`` * 15 + ``!``) demonstrates
       the engine **does** evaluate the pattern (the test is not
       skipped by some accident of configuration).
    2. The wall-clock budget asserts the engine returns within 1.0
       second on that input. The same pattern with input length 30
       has been measured at ~40 seconds on a modern CPU because
       ``(a+)+`` backtracks exponentially; this test refuses to
       ship a 40-second unit test but pins the SHAPE of the
       failure mode so a refactor that, say, accidentally feeds
       longer filenames into the engine immediately surfaces.

    If this ever times out: the rule corpus is no longer "small,
    trusted, human-curated" and the validator needs to be tightened
    (``regex.TIMEOUT``, static structural rejection, or a sandboxed
    matcher process).
    """
    engine = RuleEngine(
        rules=(
            _r(
                "redos_prone",
                # Classic catastrophic-backtracking trigger.
                match=MatchSpec(filename_regex=r"(a+)+b"),
                destination_template="Quarantine/",
            ),
        ),
    )
    # 15 a's = 2**15 = 32K partitions, completes in milliseconds.
    # 30 a's = 2**30 = 1B partitions and runs for ~40 s on this
    # machine - precisely the behaviour the docstring above warns
    # about.
    filename = ("a" * 15) + "!"
    start = time.monotonic()
    result = engine.match(filename=filename, ext=".pdf", content=None)
    elapsed = time.monotonic() - start
    assert not result.matched
    assert elapsed < 1.0, (
        f"ReDoS-prone pattern took {elapsed:.3f}s > 1.0s budget; "
        "either input length crept up or the regex engine got slower."
    )
    """A filename whose MIME stdlib can't guess fails an explicit MIME predicate."""
    engine = RuleEngine(
        rules=(
            _r(
                "pdfs_only",
                match=MatchSpec(mime_types=("application/pdf",)),
                destination_template="Documents/",
            ),
        ),
    )
    # ``no-extension`` has no extension, so guess_type returns (None, None).
    assert not engine.match(filename="no_extension", ext="", content=None).matched


def test_template_year_substitution() -> None:
    engine = RuleEngine(
        rules=(
            _r(
                "tax_year",
                match=MatchSpec(filename_regex=r"(?i)tax", ext=(".pdf",)),
                destination_template="Finance/Taxes/{year}/",
            ),
        ),
    )
    result = engine.match(filename="tax_2025.pdf", ext=".pdf", content=None)
    assert result.matched
    assert result.destination == Path("Finance/Taxes/2025")


def test_template_with_year_no_year_detected_falls_through() -> None:
    engine = RuleEngine(
        rules=(
            _r(
                "tax_year",
                match=MatchSpec(filename_regex=r"(?i)tax"),
                destination_template="Finance/Taxes/{year}/",
            ),
        ),
    )
    result = engine.match(filename="tax_form.pdf", ext=".pdf", content=None)
    assert not result.matched


def test_unknown_template_placeholder_is_unresolved() -> None:
    engine = RuleEngine(
        rules=(
            _r(
                "weird",
                match=MatchSpec(),
                destination_template="X/{unknown}/",
            ),
        ),
    )
    assert not engine.match(filename="any.pdf", ext=".pdf", content=None).matched


def test_year_match_coherence_blocks_mismatch() -> None:
    engine = RuleEngine(
        rules=(
            _r(
                "tax_2024_only",
                match=MatchSpec(filename_regex=r"(?i)tax"),
                destination_template="Taxes/2024/",
                coherence=CoherenceSpec(year_match=True),
            ),
        ),
    )
    blocked = engine.match(filename="tax_2025.pdf", ext=".pdf", content=None)
    assert not blocked.matched

    allowed = engine.match(filename="tax_2024.pdf", ext=".pdf", content=None)
    assert allowed.matched


def test_year_match_coherence_with_template_substitution_passes() -> None:
    engine = RuleEngine(
        rules=(
            _r(
                "tax_year",
                match=MatchSpec(filename_regex=r"(?i)tax"),
                destination_template="Finance/Taxes/{year}/",
                coherence=CoherenceSpec(year_match=True),
            ),
        ),
    )
    result = engine.match(filename="tax_2025.pdf", ext=".pdf", content=None)
    assert result.matched
    assert result.destination == Path("Finance/Taxes/2025")


def test_higher_score_wins_when_multiple_match() -> None:
    engine = RuleEngine(
        rules=(
            _r("low", weight=0.5, destination_template="Low/"),
            _r("high", weight=0.9, destination_template="High/"),
        ),
    )
    result = engine.match(filename="any.pdf", ext=".pdf", content=None)
    assert result.matched
    assert result.rule is not None
    assert result.rule.id == "high"


def test_year_match_requires_destination_to_contain_a_year() -> None:
    engine = RuleEngine(
        rules=(
            _r(
                "no_year_in_dest",
                match=MatchSpec(filename_regex=r"(?i)tax"),
                destination_template="Finance/",
                coherence=CoherenceSpec(year_match=True),
            ),
        ),
    )
    assert not engine.match(
        filename="tax_2025.pdf",
        ext=".pdf",
        content=None,
    ).matched
