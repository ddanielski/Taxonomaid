"""Unit tests for :class:`AppriseOutbound`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from apprise import NotifyFormat

from taxonomaid.adapters.notifiers import AppriseOutbound
from taxonomaid.domain import NotifierError

pytestmark = pytest.mark.unit


async def test_notify_pending_passes_text_body_format(monkeypatch: pytest.MonkeyPatch) -> None:
    notifier = AppriseOutbound(["json://localhost/"])

    captured: dict[str, Any] = {}

    def fake_notify(*args: Any, **kwargs: Any) -> bool:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return True

    # The adapter now keeps one ``Apprise`` per URL so it can report
    # delivery per channel; patch the inner ``notify`` of the single
    # configured channel.
    _, channel = notifier._channels[0]
    monkeypatch.setattr(channel, "notify", fake_notify)

    await notifier.notify_pending(
        decision_id="d1",
        file=Path("x.pdf"),
        proposed_destination=Path("Foo/Bar"),
        confidence=0.5,
        reason="r",
    )
    assert captured["kwargs"]["body_format"] is NotifyFormat.TEXT
    assert "<" not in captured["kwargs"]["body"]
    assert ">" not in captured["kwargs"]["body"]


async def test_notify_pending_no_op_when_no_urls() -> None:
    notifier = AppriseOutbound([])
    # Should be a clean no-op: zero channels, no exception.
    await notifier.notify_pending(
        decision_id="d",
        file=Path("x.pdf"),
        proposed_destination=Path("Foo"),
        confidence=0.5,
        reason="r",
    )


async def test_notify_pending_succeeds_when_at_least_one_channel_delivers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient failure on one channel doesn't tear down the whole notification."""
    notifier = AppriseOutbound(["json://a/", "json://b/"])

    def fail(*_args: Any, **_kwargs: Any) -> bool:
        return False

    def succeed(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(notifier._channels[0][1], "notify", fail)
    monkeypatch.setattr(notifier._channels[1][1], "notify", succeed)

    # No exception: at least one channel delivered.
    await notifier.notify_pending(
        decision_id="d2",
        file=Path("x.pdf"),
        proposed_destination=Path("Foo"),
        confidence=0.5,
        reason="r",
    )


async def test_notify_pending_raises_when_every_channel_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notifier = AppriseOutbound(["json://a/", "json://b/"])

    def fail(*_args: Any, **_kwargs: Any) -> bool:
        return False

    for _, channel in notifier._channels:
        monkeypatch.setattr(channel, "notify", fail)

    with pytest.raises(NotifierError, match="all channels failed"):
        await notifier.notify_pending(
            decision_id="d3",
            file=Path("x.pdf"),
            proposed_destination=Path("Foo"),
            confidence=0.5,
            reason="r",
        )


def test_constructor_rejects_invalid_url() -> None:
    with pytest.raises(Exception, match="apprise"):
        AppriseOutbound(["this-is-not-a-valid-apprise-url"])


def test_url_count_exposes_only_a_count() -> None:
    notifier = AppriseOutbound(["json://a/", "json://b/"])
    assert notifier.url_count == 2
    assert not hasattr(notifier, "urls")
