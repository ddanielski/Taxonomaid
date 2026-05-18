"""Unit tests for :class:`ReviewSession`."""

from __future__ import annotations

from pathlib import Path

import pytest

from taxonomaid.config import write_rules_file
from taxonomaid.domain import MatchSpec, Rule, RuleSource
from taxonomaid.services import ReviewPaths, ReviewSession

pytestmark = pytest.mark.unit


def _rule(rid: str, *, confidence: float = 0.9, samples: int = 10) -> Rule:
    return Rule(
        id=rid,
        match=MatchSpec(filename_regex=rf"(?i){rid}"),
        destination_template=f"Inbox/{rid}/",
        weight=0.7,
        confidence=confidence,
        anchored=False,
        source=RuleSource.USER_INFERRED,
        sample_count=samples,
    )


def _paths(tmp_path: Path) -> ReviewPaths:
    return ReviewPaths(
        proposed=tmp_path / "proposed_rules.yaml",
        approved=tmp_path / "rules.yaml",
        rejected=tmp_path / "rejected_rules.yaml",
    )


def test_queue_is_empty_when_no_files_exist(tmp_path: Path) -> None:
    """Fresh install: every YAML missing -> empty queue, no error."""
    session = ReviewSession(_paths(tmp_path))
    queue = session.queue()
    assert queue.is_empty
    assert queue.proposed_count == 0
    assert queue.approved_count == 0
    assert queue.rejected_count == 0


def test_queue_lists_proposals_minus_approved_minus_rejected(tmp_path: Path) -> None:
    """``Pending = proposed - approved - rejected``."""
    paths = _paths(tmp_path)
    write_rules_file(paths.proposed, (_rule("a"), _rule("b"), _rule("c")))
    write_rules_file(paths.approved, (_rule("a"),))
    write_rules_file(paths.rejected, (_rule("b"),))

    queue = ReviewSession(paths).queue()

    pending_ids = {r.id for r in queue.pending}
    assert pending_ids == {"c"}
    assert queue.proposed_count == 3
    assert queue.approved_count == 1
    assert queue.rejected_count == 1


def test_queue_orders_high_confidence_first(tmp_path: Path) -> None:
    """High-precision proposals surface first so the operator clears wins fast."""
    paths = _paths(tmp_path)
    write_rules_file(
        paths.proposed,
        (
            _rule("low", confidence=0.80, samples=5),
            _rule("high", confidence=0.99, samples=50),
            _rule("medium", confidence=0.91, samples=20),
        ),
    )
    queue = ReviewSession(paths).queue()
    assert [r.id for r in queue.pending] == ["high", "medium", "low"]


def test_approve_promotes_rule_to_approved_yaml(tmp_path: Path) -> None:
    """``approve`` adds to rules.yaml and removes from proposed_rules.yaml."""
    paths = _paths(tmp_path)
    write_rules_file(paths.proposed, (_rule("alpha"), _rule("beta")))
    session = ReviewSession(paths)

    promoted = session.approve("alpha")

    assert promoted is not None
    assert promoted.id == "alpha"
    queue = session.queue()
    assert {r.id for r in queue.pending} == {"beta"}
    assert queue.approved_count == 1


def test_reject_moves_rule_to_rejected_yaml(tmp_path: Path) -> None:
    """``reject`` adds to rejected_rules.yaml and removes from proposed_rules.yaml."""
    paths = _paths(tmp_path)
    write_rules_file(paths.proposed, (_rule("alpha"), _rule("beta")))
    session = ReviewSession(paths)

    rejected = session.reject("alpha")

    assert rejected is not None
    assert rejected.id == "alpha"
    queue = session.queue()
    assert {r.id for r in queue.pending} == {"beta"}
    assert queue.rejected_count == 1


def test_approve_unknown_proposal_id_is_idempotent(tmp_path: Path) -> None:
    """Approving an unknown id is a silent no-op, not an exception.

    Matters for the Telegram flow where a button tap might race with
    a CLI ``taxonomaid review`` invocation that already moved the
    proposal.
    """
    paths = _paths(tmp_path)
    write_rules_file(paths.proposed, (_rule("alpha"),))
    session = ReviewSession(paths)

    assert session.approve("does_not_exist") is None
    # The queue is unchanged.
    queue = session.queue()
    assert {r.id for r in queue.pending} == {"alpha"}


def test_double_approve_on_same_id_is_a_noop(tmp_path: Path) -> None:
    """Tapping ``Approve`` twice doesn't duplicate the rule in rules.yaml."""
    paths = _paths(tmp_path)
    write_rules_file(paths.proposed, (_rule("alpha"),))
    session = ReviewSession(paths)

    first = session.approve("alpha")
    second = session.approve("alpha")

    # First call succeeded.
    assert first is not None
    # Second call returns ``None`` (it's no longer pending) and
    # doesn't grow rules.yaml.
    assert second is None
    queue = session.queue()
    assert queue.is_empty
    assert queue.approved_count == 1


def test_paths_under_uses_standard_layout(tmp_path: Path) -> None:
    """``ReviewPaths.under(config_dir)`` mirrors install.sh's layout."""
    paths = ReviewPaths.under(tmp_path)
    assert paths.proposed == tmp_path / "proposed_rules.yaml"
    assert paths.approved == tmp_path / "rules.yaml"
    assert paths.rejected == tmp_path / "rejected_rules.yaml"
