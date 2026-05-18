"""Stateless review-session service for Telegram-driven rule approval.

The CLI ``taxonomaid review`` walks the operator through proposed
rules in a terminal. This module is the equivalent for the
Telegram inbound flow: when the daemon receives a ``/review``
command, it lists pending proposals; when the operator taps an
``approve`` / ``reject`` button on a proposal, this service moves
the rule between the YAML files.

The service is **stateless on purpose**. "Pending" is computed
fresh each call as
``proposed_rules.yaml minus rules.yaml minus rejected_rules.yaml``,
so:

* Daemon restarts mid-session don't lose progress.
* Two parallel ``/review`` sessions converge on the same answer
  (a rule already approved disappears from the queue for the
  second session).
* "Skip" semantics are implicit: a proposal the operator doesn't
  act on simply stays in the queue for next time.

Edits (changing the destination before approval) are deferred to
the CLI ``taxonomaid review`` command; the Telegram flow is
approve / reject only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from taxonomaid.config import load_rules_file, write_rules_file
from taxonomaid.domain import Rule


@dataclass(frozen=True, slots=True)
class ReviewQueue:
    """Snapshot of proposals waiting on the operator.

    Attributes:
        pending: Rules waiting for an answer, ordered with the
            higher-precision proposals first so the operator clears
            the easy wins fastest.
        proposed_count: Total proposals on disk (``proposed_rules.yaml``).
        approved_count: Total active rules (``rules.yaml``).
        rejected_count: Total rejected proposals
            (``rejected_rules.yaml``).
    """

    pending: tuple[Rule, ...]
    proposed_count: int
    approved_count: int
    rejected_count: int

    @property
    def is_empty(self) -> bool:
        """``True`` when no proposal is awaiting a decision."""
        return not self.pending


@dataclass(frozen=True, slots=True)
class ReviewPaths:
    """Locations of the three YAML files the review service touches."""

    proposed: Path
    approved: Path
    rejected: Path

    @classmethod
    def under(cls, config_dir: Path) -> ReviewPaths:
        """Construct paths from the operator's standard layout."""
        return cls(
            proposed=config_dir / "proposed_rules.yaml",
            approved=config_dir / "rules.yaml",
            rejected=config_dir / "rejected_rules.yaml",
        )


class ReviewSession:
    """Apply approve / reject decisions to the on-disk YAML files.

    Operations are idempotent: approving a rule that's already in
    ``rules.yaml`` is a no-op; rejecting a rule that's already
    rejected is a no-op. This matters for the Telegram flow where a
    user might double-tap a button or two daemons race on the same
    proposal_id (theoretical today; defensive).
    """

    def __init__(self, paths: ReviewPaths) -> None:
        self._paths = paths

    @property
    def paths(self) -> ReviewPaths:
        """The on-disk files this session reads + writes."""
        return self._paths

    def queue(self) -> ReviewQueue:
        """Return the current pending queue (recomputed every call)."""
        proposed = _load_optional(self._paths.proposed)
        approved = _load_optional(self._paths.approved)
        rejected = _load_optional(self._paths.rejected)
        approved_ids = {r.id for r in approved}
        rejected_ids = {r.id for r in rejected}
        pending = tuple(
            sorted(
                (r for r in proposed if r.id not in approved_ids and r.id not in rejected_ids),
                # Higher confidence + sample count first; ties break on id.
                key=lambda r: (-r.confidence, -r.sample_count, r.id),
            )
        )
        return ReviewQueue(
            pending=pending,
            proposed_count=len(proposed),
            approved_count=len(approved),
            rejected_count=len(rejected),
        )

    def approve(self, proposal_id: str) -> Rule | None:
        """Promote ``proposal_id`` from ``proposed`` to ``approved``.

        Returns the approved :class:`Rule`, or ``None`` if the
        proposal_id wasn't in the pending queue (already-applied
        idempotent case, or unknown id).
        """
        rule = self._find_pending(proposal_id)
        if rule is None:
            return None
        approved = _load_optional(self._paths.approved)
        if any(r.id == proposal_id for r in approved):
            # Already promoted by an earlier session; drop from
            # proposed and exit cleanly.
            self._remove_from_proposed(proposal_id)
            return rule
        write_rules_file(self._paths.approved, (*approved, rule))
        self._remove_from_proposed(proposal_id)
        return rule

    def reject(self, proposal_id: str) -> Rule | None:
        """Move ``proposal_id`` from ``proposed`` to ``rejected``.

        The rejected list is the miner's "don't propose this again"
        set: the rule's id stays there forever (until the operator
        edits ``rejected_rules.yaml`` by hand) so the same pattern
        won't be re-mined next week.

        Returns the rejected :class:`Rule`, or ``None`` if the
        proposal_id wasn't in the pending queue.
        """
        rule = self._find_pending(proposal_id)
        if rule is None:
            return None
        rejected = _load_optional(self._paths.rejected)
        if any(r.id == proposal_id for r in rejected):
            self._remove_from_proposed(proposal_id)
            return rule
        write_rules_file(self._paths.rejected, (*rejected, rule))
        self._remove_from_proposed(proposal_id)
        return rule

    def _find_pending(self, proposal_id: str) -> Rule | None:
        for rule in self.queue().pending:
            if rule.id == proposal_id:
                return rule
        return None

    def _remove_from_proposed(self, proposal_id: str) -> None:
        proposed = _load_optional(self._paths.proposed)
        remaining = tuple(r for r in proposed if r.id != proposal_id)
        write_rules_file(self._paths.proposed, remaining)


def _load_optional(path: Path) -> tuple[Rule, ...]:
    """Return ``load_rules_file(path)`` or ``()`` if the file is absent."""
    if not path.exists():
        return ()
    return load_rules_file(path)
