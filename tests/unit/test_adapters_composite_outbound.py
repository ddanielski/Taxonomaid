"""Unit tests for :class:`CompositeOutbound`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from taxonomaid.adapters.notifiers import CompositeOutbound
from taxonomaid.domain import NotifierError

pytestmark = pytest.mark.unit


class _RecordingOutbound:
    """Minimal :class:`NotifierOutbound`-shaped stub for tests."""

    def __init__(
        self,
        *,
        raises: BaseException | None = None,
        name: str = "RecordingOutbound",
    ) -> None:
        self.name = name
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    async def notify_pending(
        self,
        *,
        decision_id: str,
        file: Path,
        proposed_destination: Path,
        confidence: float,
        reason: str,
    ) -> None:
        self.calls.append(
            {
                "decision_id": decision_id,
                "file": file,
                "proposed_destination": proposed_destination,
                "confidence": confidence,
                "reason": reason,
            }
        )
        if self._raises is not None:
            raise self._raises

    async def aclose(self) -> None:  # pragma: no cover - exercised in tests below
        return


async def test_composite_fans_out_to_every_outbound() -> None:
    a = _RecordingOutbound(name="a")
    b = _RecordingOutbound(name="b")
    composite = CompositeOutbound([a, b])

    await composite.notify_pending(
        decision_id="d1",
        file=Path("x.pdf"),
        proposed_destination=Path("Foo"),
        confidence=0.5,
        reason="r",
    )

    assert len(a.calls) == 1
    assert len(b.calls) == 1
    assert a.calls[0]["decision_id"] == "d1"
    assert b.calls[0]["decision_id"] == "d1"


async def test_composite_tolerates_one_outbound_failing() -> None:
    """A transient outage on one channel must not fail the whole notification."""
    flaky = _RecordingOutbound(name="flaky", raises=NotifierError("transient"))
    ok = _RecordingOutbound(name="ok")
    composite = CompositeOutbound([flaky, ok])

    # Should NOT raise: the second channel delivered.
    await composite.notify_pending(
        decision_id="d2",
        file=Path("x.pdf"),
        proposed_destination=Path("Foo"),
        confidence=0.5,
        reason="r",
    )
    assert len(ok.calls) == 1


async def test_composite_raises_when_every_outbound_fails() -> None:
    a = _RecordingOutbound(name="a", raises=NotifierError("a-down"))
    b = _RecordingOutbound(name="b", raises=RuntimeError("b-broken"))
    composite = CompositeOutbound([a, b])

    with pytest.raises(NotifierError, match=r"all .* channels errored"):
        await composite.notify_pending(
            decision_id="d3",
            file=Path("x.pdf"),
            proposed_destination=Path("Foo"),
            confidence=0.5,
            reason="r",
        )


def test_composite_rejects_empty_construction() -> None:
    with pytest.raises(ValueError, match="at least one"):
        CompositeOutbound([])


def test_outbound_count_reports_wiring() -> None:
    composite = CompositeOutbound([_RecordingOutbound(), _RecordingOutbound()])
    assert composite.outbound_count == 2


async def test_aclose_calls_every_adapter_even_if_one_raises() -> None:
    """``aclose`` must not let one adapter's failure skip the others."""

    class BoomAclose:
        async def notify_pending(self, **_: Any) -> None:
            return

        async def aclose(self) -> None:
            msg = "boom"
            raise RuntimeError(msg)

    class TrackingAclose:
        def __init__(self) -> None:
            self.closed = False

        async def notify_pending(self, **_: Any) -> None:
            return

        async def aclose(self) -> None:
            self.closed = True

    tracking = TrackingAclose()
    composite = CompositeOutbound([BoomAclose(), tracking])
    await composite.aclose()
    assert tracking.closed is True
