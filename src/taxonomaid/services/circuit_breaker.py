"""Circuit breaker for LLM availability.

Why this exists
---------------

The dispatcher classifies one file at a time; on an :class:`LLMError`
it parks the file with a generic reason and sends the operator a
Telegram prompt. That's fine for a single failure - probably a
transient rate limit. It's a UX disaster for a sustained outage:
100 files arriving while Gemini is down = 100 push notifications.

The circuit breaker collapses that. After ``threshold`` consecutive
``LLMError``-level failures the circuit trips **open**: subsequent
files are parked silently (no per-file Telegram prompt) and one
"LLM unavailable" message is sent. While open, every Nth event
attempts a probe; on success the circuit closes and a single
"recovered" message is sent reporting how many files were parked
during the outage.

The breaker is in-memory by design. A daemon restart resets it,
which means a brief notification storm right after a restart that
coincides with an ongoing outage. For a personal NAS where
restarts are rare (daily at most), that's an acceptable trade
against persisting state for a transient signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

_DEFAULT_THRESHOLD: Final[int] = 3
"""Consecutive failures before the circuit trips open.

Two failures are usually a transient rate-limit retry (the OpenAI-
compat adapter already does internal exponential backoff). Three is
the smallest number that reliably distinguishes "Gemini is having a
moment" from "Gemini has been down for a minute".
"""

_DEFAULT_COOLDOWN_S: Final[float] = 60.0
"""While open, attempt a single probe at most once per cooldown.

Sixty seconds is short enough to recover quickly when Gemini
bounces back, long enough that we don't burn quota retrying every
second.
"""


class CircuitState(StrEnum):
    """Two-state circuit; "half-open" is collapsed into "open + cooldown elapsed"."""

    CLOSED = "closed"
    OPEN = "open"


@dataclass
class LLMCircuit:
    """Track LLM-call outcomes and decide when to suppress notifications.

    The dispatcher consults :meth:`allow` before every LLM call;
    reports the outcome via :meth:`record_success` /
    :meth:`record_failure`; calls :meth:`record_skip` for each file
    parked while the circuit is open. The notifier alerts on the
    state-transition return values from
    :meth:`record_failure` / :meth:`record_success`.

    Attributes:
        threshold: Consecutive failures that trip the circuit.
        cooldown_s: Min seconds between recovery probes when open.
    """

    threshold: int = _DEFAULT_THRESHOLD
    cooldown_s: float = _DEFAULT_COOLDOWN_S
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    last_open_ts: datetime | None = None
    skipped_count: int = 0

    def allow(self, now: datetime) -> bool:
        """Return ``True`` if the dispatcher should attempt the LLM call.

        ``CLOSED`` always allows. ``OPEN`` allows only when the
        cooldown has elapsed since the last open transition - that
        single probe is the "half-open" recovery test. A failure
        in that state resets the cooldown; a success closes the
        circuit (see :meth:`record_failure` / :meth:`record_success`).
        """
        if self.state is CircuitState.CLOSED:
            return True
        if self.last_open_ts is None:  # pragma: no cover - defensive
            return True
        return (now - self.last_open_ts).total_seconds() >= self.cooldown_s

    def record_failure(self, now: datetime) -> bool:
        """Record an LLM failure; return ``True`` iff the circuit just opened.

        While ``OPEN``, every additional failure resets the cooldown
        clock so a probe-and-fail cycle doesn't flap the operator's
        phone with reopen alerts.
        """
        self.consecutive_failures += 1
        if self.state is CircuitState.OPEN:
            self.last_open_ts = now
            return False
        if self.consecutive_failures >= self.threshold:
            self.state = CircuitState.OPEN
            self.last_open_ts = now
            return True
        return False

    def record_success(self) -> bool:
        """Record an LLM success; return ``True`` iff the circuit just closed."""
        was_open = self.state is CircuitState.OPEN
        self.state = CircuitState.CLOSED
        self.consecutive_failures = 0
        return was_open

    def record_skip(self) -> None:
        """Track that the dispatcher parked a file without trying the LLM.

        Called only when :meth:`allow` returned ``False``. The
        accumulated count surfaces in the recovery alert so the
        operator knows roughly how many files queued up during
        the outage.
        """
        if self.state is CircuitState.OPEN:
            self.skipped_count += 1

    def take_skipped_count(self) -> int:
        """Return the skipped count and reset the counter.

        Used by the dispatcher exactly once on a closed-transition
        so the recovery message is accurate for *this* outage and
        the next outage starts from zero.
        """
        count = self.skipped_count
        self.skipped_count = 0
        return count
