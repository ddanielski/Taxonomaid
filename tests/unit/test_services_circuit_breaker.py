"""Unit tests for :class:`LLMCircuit`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from taxonomaid.services.circuit_breaker import CircuitState, LLMCircuit

pytestmark = pytest.mark.unit


def _t(seconds: float = 0.0) -> datetime:
    """Build a deterministic UTC datetime offset by ``seconds``."""
    return datetime(2026, 5, 18, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=seconds)


def test_starts_closed_and_allows_attempts() -> None:
    """A fresh circuit is closed; ``allow`` returns True regardless of time."""
    circuit = LLMCircuit()
    assert circuit.state is CircuitState.CLOSED
    assert circuit.allow(_t())


def test_single_failure_does_not_trip_default_threshold() -> None:
    """One transient error stays under the threshold (default 3)."""
    circuit = LLMCircuit()
    just_opened = circuit.record_failure(_t())
    assert just_opened is False
    assert circuit.state is CircuitState.CLOSED


def test_threshold_consecutive_failures_open_the_circuit() -> None:
    """Default threshold is 3; the third failure trips."""
    circuit = LLMCircuit()
    assert circuit.record_failure(_t(0)) is False
    assert circuit.record_failure(_t(1)) is False
    just_opened = circuit.record_failure(_t(2))
    assert just_opened is True
    assert circuit.state is CircuitState.OPEN


def test_open_circuit_blocks_attempts_during_cooldown() -> None:
    """``allow`` returns False inside the open-cooldown window."""
    circuit = LLMCircuit(threshold=1, cooldown_s=60.0)
    circuit.record_failure(_t(0))
    assert circuit.state is CircuitState.OPEN
    # 30 s into the cooldown: still blocked.
    assert circuit.allow(_t(30.0)) is False


def test_open_circuit_allows_one_probe_after_cooldown() -> None:
    """Past the cooldown, ``allow`` returns True (the half-open probe)."""
    circuit = LLMCircuit(threshold=1, cooldown_s=60.0)
    circuit.record_failure(_t(0))
    # 60.5 s later: cooldown elapsed.
    assert circuit.allow(_t(60.5)) is True


def test_success_after_open_closes_and_signals_transition() -> None:
    """A success on the recovery probe closes the circuit and reports."""
    circuit = LLMCircuit(threshold=1)
    circuit.record_failure(_t(0))
    just_closed = circuit.record_success()
    assert just_closed is True
    assert circuit.state is CircuitState.CLOSED


def test_success_while_already_closed_does_not_signal() -> None:
    """A success in the normal-running case isn't a transition."""
    circuit = LLMCircuit()
    just_closed = circuit.record_success()
    assert just_closed is False


def test_failure_during_open_resets_cooldown() -> None:
    """A failed recovery probe pushes the cooldown clock forward.

    Without this, repeated probe-and-fail cycles would emit
    redundant "circuit opened" alerts at every threshold crossing.
    """
    circuit = LLMCircuit(threshold=1, cooldown_s=60.0)
    circuit.record_failure(_t(0))
    # Past cooldown: allow → True.
    assert circuit.allow(_t(70)) is True
    # Recovery probe fails (state stays OPEN, cooldown resets).
    just_opened = circuit.record_failure(_t(70))
    assert just_opened is False  # not a transition; was already open
    # 30 s after the failed probe: still inside the new cooldown.
    assert circuit.allow(_t(100)) is False


def test_skipped_count_only_increments_when_open() -> None:
    """``record_skip`` is a no-op in CLOSED state.

    Defensive: the dispatcher only calls ``record_skip`` when
    ``allow`` returned False, but the API guarantees correctness
    under any call order.
    """
    circuit = LLMCircuit(threshold=1)
    circuit.record_skip()  # closed; no-op
    assert circuit.skipped_count == 0
    circuit.record_failure(_t(0))
    circuit.record_skip()  # now open
    circuit.record_skip()
    assert circuit.skipped_count == 2


def test_take_skipped_count_resets() -> None:
    """``take_skipped_count`` returns the value and resets to 0."""
    circuit = LLMCircuit(threshold=1)
    circuit.record_failure(_t(0))
    circuit.record_skip()
    circuit.record_skip()
    circuit.record_skip()
    assert circuit.take_skipped_count() == 3
    assert circuit.take_skipped_count() == 0
