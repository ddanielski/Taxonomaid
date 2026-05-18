"""Composite outbound notifier.

Wraps two :class:`taxonomaid.ports.NotifierOutbound` adapters and
fans :meth:`notify_pending` out to both in parallel. Used at startup
when the operator has configured both a ``telegram:`` block (for the
inline-keyboard-rich path) **and** ``apprise_urls`` (for additional
channels - Slack, Discord, ntfy, ...). Each underlying adapter
delivers independently; the composite only raises
:class:`taxonomaid.domain.NotifierError` when *every* underlying
delivery failed.

The composite is intentionally narrow: it doesn't deduplicate, it
doesn't retry on its own, and it doesn't reorder. Those concerns
live inside the individual adapters where they belong.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

import structlog

from taxonomaid.domain import NotifierError
from taxonomaid.ports import NotifierOutbound

_log = structlog.get_logger("taxonomaid.notifier.composite")


class CompositeOutbound:
    """Fan :meth:`notify_pending` out to two or more underlying outbounds.

    Construct with the **primary** adapter first - typically
    :class:`TelegramOutbound` so the inline-keyboard UI is the user's
    preferred reply path - followed by any number of secondary
    adapters (typically a single :class:`AppriseOutbound` configured
    with non-``tgram://`` URLs).
    """

    def __init__(self, outbounds: Sequence[NotifierOutbound]) -> None:
        if not outbounds:
            msg = "CompositeOutbound requires at least one underlying outbound"
            raise ValueError(msg)
        self._outbounds: tuple[NotifierOutbound, ...] = tuple(outbounds)

    @property
    def outbound_count(self) -> int:
        """How many underlying outbounds are wired."""
        return len(self._outbounds)

    async def notify_pending(
        self,
        *,
        decision_id: str,
        file: Path,
        proposed_destination: Path,
        confidence: float,
        reason: str,
    ) -> None:
        """Send the prompt via every underlying outbound in parallel.

        Failures are isolated per outbound: a transient Slack outage
        no longer tears down a Telegram notification that already
        delivered. Raises :class:`NotifierError` only when every
        outbound failed.
        """
        results = await asyncio.gather(
            *(
                outbound.notify_pending(
                    decision_id=decision_id,
                    file=file,
                    proposed_destination=proposed_destination,
                    confidence=confidence,
                    reason=reason,
                )
                for outbound in self._outbounds
            ),
            return_exceptions=True,
        )
        succeeded = 0
        failed: list[str] = []
        for outbound, outcome in zip(self._outbounds, results, strict=True):
            adapter_name = type(outbound).__name__
            if isinstance(outcome, BaseException):
                failed.append(adapter_name)
                _log.warning(
                    "composite_outbound_failed",
                    decision_id=decision_id,
                    adapter=adapter_name,
                    error=str(outcome),
                    error_type=type(outcome).__name__,
                )
            else:
                succeeded += 1
        if succeeded == 0:
            msg = (
                f"composite outbound failed for decision_id={decision_id}; "
                f"all {len(self._outbounds)} channels errored "
                f"(adapters: {', '.join(sorted(set(failed)))})"
            )
            raise NotifierError(msg)

    async def aclose(self) -> None:
        """Close any underlying adapters that own resources."""
        for outbound in self._outbounds:
            aclose = getattr(outbound, "aclose", None)
            if aclose is None:
                continue
            try:
                await aclose()
            except Exception as exc:
                # Best-effort shutdown: an aclose failure on one
                # adapter must not block aclose on the others.
                _log.warning(
                    "composite_outbound_aclose_failed",
                    adapter=type(outbound).__name__,
                    error=str(exc),
                )
