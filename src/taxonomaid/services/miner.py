"""Pattern miner: promotes recurring decisions into rule proposals.

The miner replays the decision log, groups successful placements by
destination, and looks for stable signals - common filename tokens and
dominant extensions - that predict each destination. It produces
:class:`RuleProposal` records the CLI surfaces for human approval (and
optionally auto-promotes high-confidence proposals).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from taxonomaid.domain import (
    Decision,
    DecisionSource,
    MatchSpec,
    Rule,
    RuleSource,
)

_TOKEN_SPLIT: Final[re.Pattern[str]] = re.compile(r"[\W_]+")
_MIN_TOKEN_LEN: Final[int] = 3

_PROMOTABLE_SOURCES: Final[frozenset[DecisionSource]] = frozenset(
    {
        DecisionSource.LLM,
        DecisionSource.NOTIFIER_CONFIRMED,
        DecisionSource.USER_OVERRIDE,
    }
)


@dataclass(frozen=True, slots=True)
class RuleProposal:
    """A candidate rule the miner wants the user to approve.

    Attributes:
        rule: The proposed rule, ready to write to ``proposed_rules.yaml``.
        sample_count: How many decisions backed this proposal.
        precision: ``[0, 1]`` fraction of files matching the proposed
            predicate that landed at the proposed destination.
        sample_files: A few example filenames for review context.
        auto_promotable: ``True`` when the proposal cleared the
            stricter precision *and* sample-count thresholds. Currently
            informational only - every mined rule still flows through
            ``taxonomaid review``; auto-promotion to ``rules.yaml`` is
            not wired.
    """

    rule: Rule
    sample_count: int
    precision: float
    sample_files: tuple[str, ...]
    auto_promotable: bool


class Miner:
    """Mine rule proposals from a decision log.

    Args:
        min_samples: Minimum number of placements at a destination before
            it is considered for promotion. Default 10.
        agreement: Minimum precision (correct-destination / total-matches)
            required for a proposal. Default 0.90.
        auto_promote_threshold: Stricter precision needed for a proposal
            to be flagged as auto-promotable. Default 0.97.
        auto_promote_min_samples: Stricter sample count for
            auto-promotion. Default 30.
        proposed_weight: Weight assigned to mined rules. Default 0.7
            (below the 1.0 default for hand-written rules).
    """

    def __init__(
        self,
        *,
        min_samples: int = 10,
        agreement: float = 0.90,
        auto_promote_threshold: float = 0.97,
        auto_promote_min_samples: int = 30,
        proposed_weight: float = 0.7,
    ) -> None:
        self._min_samples = min_samples
        self._agreement = agreement
        self._auto_promote_threshold = auto_promote_threshold
        self._auto_promote_min_samples = auto_promote_min_samples
        self._proposed_weight = proposed_weight

    async def mine(
        self,
        *,
        decisions: AsyncIterator[Decision],
        existing_rules: tuple[Rule, ...] = (),
    ) -> tuple[RuleProposal, ...]:
        """Mine rule proposals from an async stream of decisions.

        Args:
            decisions: An async iterator over historical decisions
                (typically ``DecisionLog.replay()``).
            existing_rules: Currently active rules. Anchored rules
                "lock" their destination prefix - the miner skips
                proposing rules for destinations that fall under them.

        Returns:
            An immutable tuple of :class:`RuleProposal` records.
        """
        samples: list[Decision] = []
        async for decision in decisions:
            if decision.source not in _PROMOTABLE_SOURCES:
                continue
            samples.append(decision)

        anchored_prefixes = _anchored_destination_prefixes(existing_rules)
        by_destination: dict[Path, list[Decision]] = {}
        for sample in samples:
            by_destination.setdefault(sample.destination, []).append(sample)

        proposals: list[RuleProposal] = []
        for destination, group in by_destination.items():
            if len(group) < self._min_samples:
                continue
            if _under_anchored_prefix(destination, anchored_prefixes):
                continue
            proposal = self._propose(destination, group, samples)
            if proposal is not None:
                proposals.append(proposal)
        return tuple(proposals)

    def _propose(
        self,
        destination: Path,
        group: list[Decision],
        all_samples: list[Decision],
    ) -> RuleProposal | None:
        token_counts: Counter[str] = Counter()
        ext_counts: Counter[str] = Counter()
        for decision in group:
            for token in _tokenize(decision.file.name):
                token_counts[token] += 1
            ext = Path(decision.file.name).suffix.lower()
            if ext:
                ext_counts[ext] += 1

        n = len(group)
        candidate_tokens = [
            token for token, count in token_counts.items() if count / n >= self._agreement
        ]
        if not candidate_tokens:
            return None

        best_token = max(candidate_tokens, key=lambda t: token_counts[t])
        # Only enforce a dominant extension when it shares the same
        # agreement bar as the token. Without this, a destination with
        # 30 PDFs and 25 docs would propose `ext=[.pdf]` and the docs
        # would never match the rule.
        #
        # Side-effect: when no extension clears the bar we emit
        # `ext=None`, which means the rule will fire on extensions
        # that have never been seen at this destination. The
        # subsequent precision recheck (`match_at_destination /
        # match_total >= agreement`) over historical samples bounds
        # the false-positive rate, but a user who genuinely wants
        # extension-strict rules should hand-edit `proposed_rules.yaml`
        # before approving via `taxonomaid review`.
        dominant_ext: str | None = None
        if ext_counts:
            ext_top, ext_top_count = ext_counts.most_common(1)[0]
            if ext_top_count / n >= self._agreement:
                dominant_ext = ext_top
        # Letter-boundary lookarounds: `\b` doesn't trigger after `_`
        # (underscore is a word character in Python regex), so we use
        # explicit "not a letter" guards instead. This makes `tax`
        # match `tax_2025.pdf` but not `syntax_2025.pdf` or
        # `taxonomy.pdf`.
        regex = rf"(?i)(?<![A-Za-z]){re.escape(best_token)}(?![A-Za-z])"
        ext_filter = (dominant_ext,) if dominant_ext else None

        match_total = 0
        match_at_destination = 0
        for sample in all_samples:
            filename = sample.file.name
            if re.search(regex, filename) is None:
                continue
            if ext_filter is not None and Path(filename).suffix.lower() not in ext_filter:
                continue
            match_total += 1
            if sample.destination == destination:
                match_at_destination += 1

        if match_total == 0:
            return None
        precision = match_at_destination / match_total
        if precision < self._agreement:
            return None

        auto_promotable = (
            precision >= self._auto_promote_threshold
            and match_at_destination >= self._auto_promote_min_samples
        )
        # Mined rules are always tagged USER_INFERRED until a human
        # approves them via ``taxonomaid review``. The auto_promotable
        # flag on RuleProposal is informational; auto-promotion to
        # ``rules.yaml`` without review is not currently wired.
        rule = Rule(
            id=_proposal_id(best_token, dominant_ext, destination),
            match=MatchSpec(filename_regex=regex, ext=ext_filter),
            destination_template=_destination_template(destination),
            weight=self._proposed_weight,
            confidence=precision,
            anchored=False,
            source=RuleSource.USER_INFERRED,
            sample_count=match_at_destination,
        )
        return RuleProposal(
            rule=rule,
            sample_count=match_at_destination,
            precision=precision,
            sample_files=tuple(d.file.name for d in group[:5]),
            auto_promotable=auto_promotable,
        )


def _tokenize(filename: str) -> set[str]:
    stem = Path(filename).stem.lower()
    parts = _TOKEN_SPLIT.split(stem)
    return {p for p in parts if len(p) >= _MIN_TOKEN_LEN and not p.isdigit()}


def _proposal_id(token: str, ext: str | None, destination: Path) -> str:
    sanitised = re.sub(r"[^a-z0-9]+", "_", str(destination).lower()).strip("_")
    ext_part = (ext or "any").lstrip(".")
    return f"mined_{token}_{ext_part}_{sanitised}"


def _destination_template(destination: Path) -> str:
    text = str(destination)
    return text if text.endswith("/") else text + "/"


def _anchored_destination_prefixes(rules: tuple[Rule, ...]) -> tuple[Path, ...]:
    prefixes: list[Path] = []
    for rule in rules:
        if not rule.anchored:
            continue
        bare = re.sub(r"\{[^}]*\}.*$", "", rule.destination_template).rstrip("/")
        if bare:
            prefixes.append(Path(bare))
    return tuple(prefixes)


def _under_anchored_prefix(destination: Path, prefixes: tuple[Path, ...]) -> bool:
    """Return ``True`` when ``destination`` falls under any anchored prefix.

    The decision log records absolute destinations
    (``/srv/share/Documents/Finance/Taxes/2025``) but anchored rule
    templates yield relative prefixes (``Finance/Taxes``) - a naive
    ``startswith`` therefore never matches and the "anchored rules
    are never modified" guarantee silently fails. We instead check
    whether the prefix's path components appear as a contiguous
    subsequence anywhere in the destination's components, so the
    same prefix matches both ``Finance/Taxes/2025`` (relative) and
    ``/srv/share/Documents/Finance/Taxes/2025`` (absolute).

    The trade-off is that this is intentionally permissive: any
    destination containing the anchored prefix's components in order
    counts as protected. That matches the Phase-2 spec - an anchored
    rule "owns" the entire subtree, regardless of how it was reached.
    """
    dest_parts = destination.parts
    for prefix in prefixes:
        prefix_parts = prefix.parts
        if not prefix_parts:
            continue
        n = len(prefix_parts)
        if n > len(dest_parts):
            continue
        for i in range(len(dest_parts) - n + 1):
            if dest_parts[i : i + n] == prefix_parts:
                return True
    return False
