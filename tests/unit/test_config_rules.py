"""Unit tests for the rule schema, loader, and domain conversion."""

from __future__ import annotations

from pathlib import Path

import pytest

from taxonomaid.config import RuleConfig, RulesConfig, load_rules_file
from taxonomaid.config.models import MatchSpecConfig, WatchConfig, _looks_redos_prone
from taxonomaid.domain import ConfigError, RuleSource

pytestmark = pytest.mark.unit


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_match_spec_rejects_invalid_regex() -> None:
    with pytest.raises(ValueError, match="filename_regex"):
        MatchSpecConfig(filename_regex="(unbalanced")


@pytest.mark.parametrize(
    "pattern",
    [
        # Classic nested-quantifier ReDoS triggers.
        r"(a+)+b",
        r"(a*)*$",
        r"(\w+)+@example\.com",
        r"(.+)+x",
    ],
)
def test_match_spec_warns_on_redos_prone_regex(pattern: str) -> None:
    """M5 regression: nested quantifiers surface a structured warning at load."""
    spec = MatchSpecConfig(filename_regex=pattern)
    # The pattern is still accepted - the warning is the safety net,
    # not a rejection. The rule corpus is trusted input.
    assert spec.filename_regex == pattern
    # The heuristic itself is what we're pinning; the structured-log
    # delivery is exercised in :mod:`test_logging` and via the
    # structured logger directly here.
    assert _looks_redos_prone(pattern) is True


@pytest.mark.parametrize(
    "pattern",
    [
        r"^invoice_\d+\.pdf$",
        r"^[A-Za-z]+_\d{4}\.docx$",
        r".*receipt.*",
    ],
)
def test_match_spec_does_not_warn_on_benign_regex(pattern: str) -> None:
    MatchSpecConfig(filename_regex=pattern)
    assert _looks_redos_prone(pattern) is False


def test_match_spec_normalises_extensions() -> None:
    spec = MatchSpecConfig(ext=(".PDF", ".Docx"))
    assert spec.ext == (".pdf", ".docx")


def test_match_spec_rejects_extension_without_leading_dot() -> None:
    with pytest.raises(ValueError, match="leading dot"):
        MatchSpecConfig(ext=("pdf",))


@pytest.mark.parametrize(
    "template",
    [
        "Finance/{quarter}/",
        "Documents/{vendor}/{year}/",
        "{unknown}/",
    ],
)
def test_destination_template_rejects_unknown_placeholder(template: str) -> None:
    with pytest.raises(ValueError, match="unknown placeholder"):
        RuleConfig(
            id="r",
            match=MatchSpecConfig(),
            destination_template=template,
        )


def test_destination_template_accepts_year_placeholder() -> None:
    rule = RuleConfig(
        id="r",
        match=MatchSpecConfig(),
        destination_template="Finance/Taxes/{year}/",
    )
    assert rule.destination_template == "Finance/Taxes/{year}/"


def test_destination_template_accepts_no_placeholder() -> None:
    rule = RuleConfig(
        id="r",
        match=MatchSpecConfig(),
        destination_template="Finance/Receipts/",
    )
    assert rule.destination_template == "Finance/Receipts/"


def test_watch_config_rejects_unsorted_dir_matching_root_basename() -> None:
    """L-new-1 regression: a pathological config that would collapse the tray.

    ``safe_resolve`` strips one redundant copy of
    ``destination_root.name`` from the front of a candidate path. If
    ``destination_root: /tmp/_unsorted`` is paired with
    ``unsorted_dir: _unsorted``, the strip collapses ``_unsorted/``
    to an empty path - and parked files would land at the watch root
    instead of inside the tray. We refuse the combination at config-
    load time.
    """
    with pytest.raises(ValueError, match="cannot share its name"):
        WatchConfig(
            path=Path("/tmp/_unsorted/source"),
            destination_root=Path("/tmp/_unsorted"),
            unsorted_dir=Path("_unsorted"),
        )


def test_watch_config_accepts_distinct_unsorted_dir() -> None:
    """Sanity check: the validator only fires on the pathological case."""
    watch = WatchConfig(
        path=Path("/tmp/Documents"),
        destination_root=Path("/tmp/Documents"),
        unsorted_dir=Path("_unsorted"),
    )
    assert watch.unsorted_dir == Path("_unsorted")


def test_rule_config_to_domain_round_trip() -> None:
    cfg = RuleConfig(
        id="r1",
        match=MatchSpecConfig(filename_regex=r".*", ext=(".pdf",)),
        destination_template="Misc/",
        weight=0.7,
        confidence=0.9,
        anchored=True,
        source=RuleSource.AUTO_PROMOTED,
        sample_count=12,
    )
    rule = cfg.to_domain()
    assert rule.id == "r1"
    assert rule.match.ext == (".pdf",)
    assert rule.score == pytest.approx(0.7 * 0.9)
    assert rule.anchored is True
    assert rule.source is RuleSource.AUTO_PROMOTED


def test_rules_config_rejects_duplicate_ids() -> None:
    a = RuleConfig(
        id="dup",
        match=MatchSpecConfig(),
        destination_template="A/",
    )
    b = RuleConfig(
        id="dup",
        match=MatchSpecConfig(),
        destination_template="B/",
    )
    with pytest.raises(ValueError, match="duplicate rule id"):
        RulesConfig(rules=(a, b))


def test_load_rules_file_full_round_trip(tmp_path: Path) -> None:
    body = """
rules:
  - id: tax_pdf_to_year
    match:
      filename_regex: '(?i)\\\\b(tax|irs|1040|w-?2)\\\\b'
      ext: [.pdf]
    destination_template: 'Finance/Taxes/{year}/'
    coherence:
      year_match: true
    weight: 1.0
    anchored: true
    source: hand
    confidence: 1.0
    sample_count: 0
"""
    rules_path = tmp_path / "rules.yaml"
    _write(rules_path, body)

    rules = load_rules_file(rules_path)
    assert len(rules) == 1
    rule = rules[0]
    assert rule.id == "tax_pdf_to_year"
    assert rule.coherence.year_match is True
    assert rule.anchored is True
    assert rule.match.ext == (".pdf",)


def test_load_rules_file_invalid_regex_raises_config_error(tmp_path: Path) -> None:
    body = """
rules:
  - id: bad
    match:
      filename_regex: '(unbalanced'
    destination_template: 'X/'
"""
    rules_path = tmp_path / "rules.yaml"
    _write(rules_path, body)

    with pytest.raises(ConfigError, match="filename_regex"):
        load_rules_file(rules_path)


def test_load_rules_file_empty(tmp_path: Path) -> None:
    rules_path = tmp_path / "rules.yaml"
    _write(rules_path, "rules: []\n")
    assert load_rules_file(rules_path) == ()


def test_load_rules_file_completely_empty(tmp_path: Path) -> None:
    rules_path = tmp_path / "rules.yaml"
    _write(rules_path, "")
    assert load_rules_file(rules_path) == ()


def test_load_rules_file_example_template_parses() -> None:
    example = Path(__file__).parents[2] / "config" / "rules.example.yaml"
    rules = load_rules_file(example)
    assert len(rules) == 2
    ids = {r.id for r in rules}
    assert ids == {"tax_pdf_to_year", "receipts_pdf"}
