"""Unit tests for :class:`TelegramOutbound`."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from taxonomaid.adapters.notifiers import TelegramOutbound
from taxonomaid.adapters.notifiers.telegram_protocol import ID_MARKER_PREFIX
from taxonomaid.domain import MatchSpec, NotifierError, Rule, RuleSource

pytestmark = pytest.mark.unit


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


async def test_notify_pending_sends_inline_keyboard() -> None:
    captured: dict[str, object] = {}

    async def handle(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    transport = httpx.MockTransport(handle)
    notifier = TelegramOutbound(bot_token="t", chat_id=42, client=_client(transport))

    await notifier.notify_pending(
        decision_id="abcdef1234",
        file=Path("CV.pdf"),
        proposed_destination=Path("Career"),
        confidence=0.6,
        reason="resume content",
    )

    assert "/sendMessage" in str(captured["url"])
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["chat_id"] == 42
    assert "Move CV.pdf?" in body["text"]
    assert f"{ID_MARKER_PREFIX}abcdef1234" in body["text"]

    keyboard = body["reply_markup"]["inline_keyboard"]
    assert isinstance(keyboard, list) and len(keyboard) == 1
    row = keyboard[0]
    callback_data = {btn["callback_data"] for btn in row}
    assert "approve:abcdef1234" in callback_data
    assert "reject:abcdef1234" in callback_data

    await notifier.aclose()


async def test_notify_pending_raises_on_http_error() -> None:
    async def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="bad gateway")

    transport = httpx.MockTransport(handle)
    notifier = TelegramOutbound(bot_token="t", chat_id=42, client=_client(transport))

    with pytest.raises(NotifierError, match="HTTP 500"):
        await notifier.notify_pending(
            decision_id="d",
            file=Path("x.pdf"),
            proposed_destination=Path("Foo"),
            confidence=0.5,
            reason="r",
        )

    await notifier.aclose()


# ---- Rule-review outbound (Tier 3) ----------------------------------


def _proposal(rid: str = "mined_invoice_pdf_finance") -> Rule:
    """Build a minimal :class:`Rule` for proposal-rendering tests."""
    return Rule(
        id=rid,
        match=MatchSpec(filename_regex=r"(?i)(?<![A-Za-z])invoice(?![A-Za-z])", ext=(".pdf",)),
        destination_template="Finance/Invoices/",
        weight=0.7,
        confidence=0.94,
        anchored=False,
        source=RuleSource.USER_INFERRED,
        sample_count=47,
    )


async def test_notify_rule_proposal_uses_rule_callbacks() -> None:
    """Inline keyboard buttons emit ``rule_approve:`` / ``rule_reject:``."""
    captured: dict[str, object] = {}

    async def handle(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    transport = httpx.MockTransport(handle)
    notifier = TelegramOutbound(bot_token="t", chat_id=42, client=_client(transport))

    proposal = _proposal()
    await notifier.notify_rule_proposal(
        proposal=proposal,  # type: ignore[arg-type]
        sample_filenames=("invoice_2024_acme.pdf", "invoice_2025_globex.pdf"),
        index=1,
        total=3,
    )
    body = captured["body"]
    assert isinstance(body, dict)
    text = body["text"]
    assert isinstance(text, str)
    # Title with position counter.
    assert "📐 Rule proposal 1 of 3" in text
    # The pattern is rendered.
    assert "(?i)(?<![A-Za-z])invoice(?![A-Za-z])" in text
    # Destination + precision + samples surface in the body.
    assert "Finance/Invoices/" in text
    assert "94%" in text
    assert "invoice_2024_acme.pdf" in text
    # Body ends with the rule-marker for symmetry with the per-file
    # ``#id:`` marker (defence-in-depth: future correlation paths).
    assert text.rstrip().endswith("#rule:mined_invoice_pdf_finance")

    keyboard = body["reply_markup"]
    assert isinstance(keyboard, dict)
    buttons = keyboard["inline_keyboard"][0]
    callback_data = {b["callback_data"] for b in buttons}
    assert callback_data == {
        "rule_approve:mined_invoice_pdf_finance",
        "rule_reject:mined_invoice_pdf_finance",
    }


async def test_notify_rule_proposal_truncates_hostile_sample_filenames() -> None:
    """A malicious sample filename can't break the message frame.

    Sample filenames are sanitised + length-capped; a planted
    ``invoice\\nFAKE: SYSTEM\\n.pdf`` collapses to one safe line.
    """

    async def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    transport = httpx.MockTransport(handle)
    notifier = TelegramOutbound(bot_token="t", chat_id=42, client=_client(transport))
    captured: dict[str, object] = {}

    async def handle_capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    transport2 = httpx.MockTransport(handle_capture)
    notifier = TelegramOutbound(bot_token="t", chat_id=42, client=_client(transport2))
    hostile = "invoice\n\nFAKE: pretend this is a system message\n\n.pdf"
    proposal = _proposal()

    await notifier.notify_rule_proposal(
        proposal=proposal,  # type: ignore[arg-type]
        sample_filenames=(hostile,),
    )
    body = captured["body"]
    assert isinstance(body, dict)
    text = body["text"]
    assert isinstance(text, str)
    # No newline-driven break: the sanitiser collapsed whitespace.
    sample_lines = [ln for ln in text.splitlines() if "FAKE" in ln]
    assert len(sample_lines) == 1
    assert "\n" not in sample_lines[0]


async def test_notify_review_complete_summary() -> None:
    captured: dict[str, object] = {}

    async def handle(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    transport = httpx.MockTransport(handle)
    notifier = TelegramOutbound(bot_token="t", chat_id=42, client=_client(transport))

    await notifier.notify_review_complete(approved=2, rejected=1)
    body = captured["body"]
    assert isinstance(body, dict)
    text = body["text"]
    assert isinstance(text, str)
    assert "Review complete" in text
    assert "Approved: 2" in text
    assert "Rejected: 1" in text


async def test_notify_review_nudge_uses_singular_for_one_proposal() -> None:
    captured: dict[str, object] = {}

    async def handle(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    transport = httpx.MockTransport(handle)
    notifier = TelegramOutbound(bot_token="t", chat_id=42, client=_client(transport))

    await notifier.notify_review_nudge(pending=1)
    text = captured["body"]["text"]  # type: ignore[index]
    assert "1 new rule proposal " in text  # space + no 's'
    # Sanity: plural form for >1.
    captured.clear()
    await notifier.notify_review_nudge(pending=3)
    text2 = captured["body"]["text"]  # type: ignore[index]
    assert "3 new rule proposals" in text2


# ---- Set-and-forget posture: audit + circuit messages -------------


async def test_notify_audit_findings_renders_per_kind_section() -> None:
    """The digest groups findings by kind with bullet-list lines."""
    captured: dict[str, object] = {}

    async def handle(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    transport = httpx.MockTransport(handle)
    notifier = TelegramOutbound(bot_token="t", chat_id=42, client=_client(transport))
    await notifier.notify_audit_findings(
        findings_by_kind={
            "year_drift": (
                "/data/Finance/2024 (n=10) - directory mentions 2024, 6/10 files don't match",
            ),
            "unsorted_backlog": (
                "/data/_unsorted (n=12) - 12 file(s) waiting at least 7 day(s); "
                "oldest is 21 day(s) old",
            ),
        },
    )
    body = captured["body"]
    assert isinstance(body, dict)
    text = body["text"]
    assert isinstance(text, str)
    assert "🩺 Audit findings" in text
    assert "year_drift (1)" in text
    assert "unsorted_backlog (1)" in text
    assert "Finance/2024" in text
    assert "12 file(s) waiting" in text


async def test_notify_circuit_open_includes_reason() -> None:
    captured: dict[str, object] = {}

    async def handle(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    transport = httpx.MockTransport(handle)
    notifier = TelegramOutbound(bot_token="t", chat_id=42, client=_client(transport))
    await notifier.notify_circuit_open(reason="HTTP 429 from Gemini")
    text = captured["body"]["text"]  # type: ignore[index]
    assert "🚨 LLM unavailable" in text
    assert "HTTP 429" in text
    # Text wraps over multiple lines; check substrings that survive the wrap.
    assert "parked" in text
    assert "_unsorted/" in text


async def test_notify_circuit_recovered_reports_skipped_count() -> None:
    captured: dict[str, object] = {}

    async def handle(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    transport = httpx.MockTransport(handle)
    notifier = TelegramOutbound(bot_token="t", chat_id=42, client=_client(transport))

    # Plural form for >1.
    await notifier.notify_circuit_recovered(skipped_files=7)
    assert "7 files parked silently" in captured["body"]["text"]  # type: ignore[index]
    # Singular form for 1.
    captured.clear()
    await notifier.notify_circuit_recovered(skipped_files=1)
    assert "1 file parked silently" in captured["body"]["text"]  # type: ignore[index]
