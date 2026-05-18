"""Rule definitions consumed by the rule engine.

A :class:`Rule` is the cheap, deterministic alternative to invoking the LLM.
The miner promotes patterns from ``decisions.jsonl`` into rules; the auditor
demotes rules whose destinations have lost coherence. Anchored rules are
protected from both auto-modify paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class RuleSource(StrEnum):
    """Provenance of a rule, used by the miner and auditor."""

    HAND = "hand"
    AUTO_PROMOTED = "auto_promoted"
    USER_INFERRED = "user_inferred"
    NOTIFIER_CONFIRMED = "notifier_confirmed"


@dataclass(frozen=True, slots=True)
class MatchSpec:
    """Predicate side of a :class:`Rule`.

    All fields are conjunctive: every non-``None`` field must match for the
    rule to fire. ``None`` means "ignore this dimension".

    Attributes:
        filename_regex: Case-sensitive regex; use ``(?i)`` inline flag for
            case-insensitive.
        ext: Allowed extensions, stored with leading dot
            (e.g. ``[".pdf", ".docx"]``).
        mime_types: Allowed MIME types (e.g. ``["application/pdf"]``).
        content_keywords: Plain substrings that must appear in the extracted
            text content.
    """

    filename_regex: str | None = None
    ext: tuple[str, ...] | None = None
    mime_types: tuple[str, ...] | None = None
    content_keywords: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class CoherenceSpec:
    """Structural guards beyond simple filename matching.

    These prevent surface-level matches from driving incorrect placements -
    e.g., a tax PDF whose detected year doesn't match the destination's
    ``{year}`` placeholder.
    """

    year_match: bool = False


@dataclass(frozen=True, slots=True)
class Rule:
    """A scored, optionally anchored placement rule.

    Selection: the dispatcher picks the rule with the highest
    ``weight * confidence`` whose :class:`CoherenceSpec` checks pass; if
    none pass, control hands off to the LLM.

    Attributes:
        id: Stable identifier; auto-generated rules use ``mined_<hash>``.
        match: The predicate.
        destination_template: Path template. ``{year}`` is the only
            placeholder currently substituted (extracted from the
            filename / excerpt by
            :func:`taxonomaid.services.year_extractor.detect_year`). A
            template containing any other placeholder will fail
            resolution and the rule won't fire.
        coherence: Structural guards.
        weight: Static priority, ``[0, +inf)``. Hand rules default to ``1.0``.
        confidence: Dynamic, learned score in ``[0, 1]``. Mined rules start
            at the miner's promotion threshold.
        anchored: When ``True`` the rule is immune to auto-demote /
            auto-modify by the miner and auditor.
        source: Where the rule came from.
        sample_count: Number of decisions that fed into the confidence
            score; used for weighting and demotions.
    """

    id: str
    match: MatchSpec
    destination_template: str
    coherence: CoherenceSpec = field(default_factory=CoherenceSpec)
    weight: float = 1.0
    confidence: float = 1.0
    anchored: bool = False
    source: RuleSource = RuleSource.HAND
    sample_count: int = 0

    def __post_init__(self) -> None:
        """Validate scalar bounds."""
        if not 0.0 <= self.confidence <= 1.0:
            msg = f"Rule.confidence must be in [0, 1]; got {self.confidence!r}"
            raise ValueError(msg)
        if self.weight < 0.0:
            msg = f"Rule.weight must be >= 0; got {self.weight!r}"
            raise ValueError(msg)
        if self.sample_count < 0:
            msg = f"Rule.sample_count must be >= 0; got {self.sample_count!r}"
            raise ValueError(msg)

    @property
    def score(self) -> float:
        """Effective selection score (``weight * confidence``)."""
        return self.weight * self.confidence
