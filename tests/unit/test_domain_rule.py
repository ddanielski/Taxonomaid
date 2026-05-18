"""Unit tests for :mod:`taxonomaid.domain.rule`."""

from __future__ import annotations

import pytest

from taxonomaid.domain import CoherenceSpec, MatchSpec, Rule, RuleSource

pytestmark = pytest.mark.unit


def _rule(**overrides: object) -> Rule:
    defaults: dict[str, object] = {
        "id": "test_rule",
        "match": MatchSpec(filename_regex=r".*"),
        "destination_template": "Misc/",
    }
    defaults.update(overrides)
    return Rule(**defaults)  # type: ignore[arg-type]


def test_rule_score_is_weight_times_confidence() -> None:
    rule = _rule(weight=0.8, confidence=0.5)
    assert rule.score == pytest.approx(0.4)


def test_rule_default_coherence_is_empty() -> None:
    rule = _rule()
    assert rule.coherence == CoherenceSpec()
    assert rule.source is RuleSource.HAND
    assert rule.anchored is False


@pytest.mark.parametrize(
    "field,value", [("confidence", -0.1), ("weight", -1.0), ("sample_count", -1)]
)
def test_rule_validates_bounds(field: str, value: float | int) -> None:
    with pytest.raises(ValueError, match=field):
        _rule(**{field: value})
