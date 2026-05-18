"""Unit tests for the rules YAML writer."""

from __future__ import annotations

from pathlib import Path

import pytest

from taxonomaid.config import load_rules_file, write_rules_file
from taxonomaid.domain import CoherenceSpec, MatchSpec, Rule, RuleSource

pytestmark = pytest.mark.unit


def test_write_then_load_round_trips(tmp_path: Path) -> None:
    rule = Rule(
        id="tax_pdf_to_year",
        match=MatchSpec(filename_regex=r"(?i)tax", ext=(".pdf",)),
        destination_template="Finance/Taxes/{year}/",
        coherence=CoherenceSpec(year_match=True),
        weight=0.9,
        confidence=0.97,
        anchored=True,
        source=RuleSource.AUTO_PROMOTED,
        sample_count=42,
    )
    out = tmp_path / "rules.yaml"
    write_rules_file(out, (rule,))
    loaded = load_rules_file(out)
    assert len(loaded) == 1
    assert loaded[0] == rule


def test_write_empty_rules_produces_loadable_file(tmp_path: Path) -> None:
    out = tmp_path / "rules.yaml"
    write_rules_file(out, ())
    assert load_rules_file(out) == ()


def test_write_omits_unset_match_fields(tmp_path: Path) -> None:
    rule = Rule(
        id="ext_only",
        match=MatchSpec(ext=(".pdf",)),
        destination_template="Pdfs/",
    )
    out = tmp_path / "rules.yaml"
    write_rules_file(out, (rule,))
    text = out.read_text(encoding="utf-8")
    assert "filename_regex" not in text
    assert "mime_types" not in text
