"""Unit tests for the Telegram inbound parser and update handler."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from taxonomaid.adapters.notifiers import TelegramInbound
from taxonomaid.adapters.notifiers.telegram_inbound import (
    _MAX_PROPOSED_PATH_CHARS,
    extract_decision_id,
    looks_like_relative_path,
    parse_callback_data,
    parse_reply,
)
from taxonomaid.adapters.notifiers.telegram_protocol import ID_MARKER_PREFIX
from taxonomaid.ports import NotifierResponseKind

pytestmark = pytest.mark.unit


def test_parse_approve_command() -> None:
    response = parse_reply("/approve abc123")
    assert response is not None
    assert response.kind is NotifierResponseKind.APPROVE
    assert response.decision_id == "abc123"


def test_parse_reject_command() -> None:
    response = parse_reply("/reject xyz")
    assert response is not None
    assert response.kind is NotifierResponseKind.REJECT


def test_parse_move_command_with_path() -> None:
    response = parse_reply("/move abc Documents/Foo/")
    assert response is not None
    assert response.kind is NotifierResponseKind.PROPOSE
    assert response.proposed_destination == Path("Documents/Foo/")


def test_parse_returns_none_for_non_command() -> None:
    assert parse_reply("hello there") is None


def test_parse_returns_none_for_empty_input() -> None:
    assert parse_reply("") is None


def test_parse_returns_none_for_command_missing_args() -> None:
    assert parse_reply("/approve") is None
    assert parse_reply("/move only-id") is None


def test_parse_is_case_insensitive_on_command() -> None:
    response = parse_reply("/Approve ID")
    assert response is not None
    assert response.kind is NotifierResponseKind.APPROVE


def test_parse_callback_data_approve() -> None:
    response = parse_callback_data("approve:abc123")
    assert response is not None
    assert response.kind is NotifierResponseKind.APPROVE
    assert response.decision_id == "abc123"


def test_parse_callback_data_reject() -> None:
    response = parse_callback_data("reject:xyz")
    assert response is not None
    assert response.kind is NotifierResponseKind.REJECT


def test_parse_callback_data_unknown_action_returns_none() -> None:
    assert parse_callback_data("delete:abc") is None
    assert parse_callback_data("approve:") is None
    assert parse_callback_data("nonsense") is None


def test_extract_decision_id_finds_marker() -> None:
    text = f"Move CV.pdf?\nProposed: Career\n\n{ID_MARKER_PREFIX}064f5d29861248dda7519f237d"
    assert extract_decision_id(text) == "064f5d29861248dda7519f237d"


def test_extract_decision_id_returns_none_when_absent() -> None:
    assert extract_decision_id("just some text") is None


@pytest.mark.parametrize(
    "text",
    [
        "ok",
        "ok thanks",
        "yes please",
        "no.",
        "Career",
        "👍",
        "this looks great!",
    ],
)
def test_looks_like_relative_path_rejects_chat_replies(text: str) -> None:
    assert looks_like_relative_path(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "Career/CVs",
        "Career/CVs/2026",
        "Finance/Taxes 2025/Receipts",
        "a/b",
    ],
)
def test_looks_like_relative_path_accepts_paths(text: str) -> None:
    assert looks_like_relative_path(text) is True


def test_looks_like_relative_path_rejects_traversal_and_absolute() -> None:
    assert looks_like_relative_path("../escape") is False
    assert looks_like_relative_path("foo/../escape") is False
    assert looks_like_relative_path("/abs/path") is False


@pytest.mark.parametrize(
    "text",
    [
        # Non-ASCII paths the user genuinely types on a personal NAS.
        "Documenti/Casa",
        "Steuern/2025",
        "経理/2025/領収書",
        "Acta/2025",
    ],
)
def test_looks_like_relative_path_accepts_unicode(text: str) -> None:
    assert looks_like_relative_path(text) is True


@pytest.mark.parametrize(
    "text",
    [
        # Zero-width joiner inside a segment.
        "Reports/Q\u200d1",
        # Right-to-left override (BiDi).
        "Reports/\u202eName",
        # Soft hyphen (Cf "Format").
        "Reports/My\u00adFolder",
    ],
)
def test_looks_like_relative_path_rejects_invisible_chars(text: str) -> None:
    """H9 regression: invisible characters never make it into a folder name."""
    assert looks_like_relative_path(text) is False


def test_id_marker_anchored_to_line_boundary() -> None:
    """4.2 regression: a filename can't spoof the trailing #id marker.

    The bot's outbound message ends with ``#id:<id>`` on its own line.
    A malicious filename like ``report_#id:dead00.pdf`` would
    otherwise produce a non-anchored ``#id:`` substring earlier in
    the message that ``re.search`` would match first, causing the
    inbound listener to discard the user's reply as
    ``notifier_unknown_decision``.

    The fix anchors the regex to a line boundary and returns the
    LAST match, so the legitimate trailing marker always wins.
    """
    spoofed_message = (
        "📁 Move report_#id:dead0000.pdf?\n"
        "Proposed: Career/CVs\n"
        "Confidence: 0.75\n"
        "Reason: looks like a CV\n"
        "\n"
        "Tap Approve / Reject, or reply to this message with a custom path.\n"
        "\n"
        "#id:abcdef1234567890\n"
    )
    assert extract_decision_id(spoofed_message) == "abcdef1234567890"


def test_id_marker_only_recognises_line_anchored_form() -> None:
    """An in-body ``#id:`` substring with trailing junk doesn't match."""
    # The trailing ``.pdf?`` breaks the anchor: the line ends with
    # extra characters, so this no longer satisfies the regex.
    in_body_only = "Move report_#id:dead0000.pdf? Hello world"
    assert extract_decision_id(in_body_only) is None


def test_move_path_length_cap() -> None:
    """4.4 regression: an overlong /move path is refused early."""
    long_path = "a/" * 3000  # ~6000 chars, well over the 4096 cap
    assert len(long_path) > _MAX_PROPOSED_PATH_CHARS
    assert parse_reply(f"/move abc {long_path}") is None


def test_move_path_under_cap_is_accepted() -> None:
    response = parse_reply("/move abc Career/CVs")
    assert response is not None
    assert str(response.proposed_destination) == "Career/CVs"


# ---- Rule-review parsing (Tier 3 Telegram review flow) --------------


def test_slash_review_command_yields_review_start() -> None:
    """``/review`` is the queue-walk trigger; no id required."""
    response = parse_reply("/review")
    assert response is not None
    assert response.kind is NotifierResponseKind.REVIEW_START
    assert response.decision_id == ""


def test_slash_review_tolerates_trailing_text() -> None:
    """Extra tokens after ``/review`` don't confuse the parser."""
    response = parse_reply("/review please")
    assert response is not None
    assert response.kind is NotifierResponseKind.REVIEW_START


def test_rule_approve_callback_parses() -> None:
    """``rule_approve:<proposal_id>`` carries the id in ``decision_id``."""
    response = parse_callback_data("rule_approve:mined_invoice_pdf_finance")
    assert response is not None
    assert response.kind is NotifierResponseKind.RULE_APPROVE
    assert response.decision_id == "mined_invoice_pdf_finance"


def test_rule_reject_callback_parses() -> None:
    response = parse_callback_data("rule_reject:mined_xyz")
    assert response is not None
    assert response.kind is NotifierResponseKind.RULE_REJECT
    assert response.decision_id == "mined_xyz"


def test_per_file_callbacks_are_distinct_from_rule_callbacks() -> None:
    """``approve:`` is per-file; ``rule_approve:`` is per-rule. Two namespaces."""
    file_response = parse_callback_data("approve:abc")
    rule_response = parse_callback_data("rule_approve:abc")
    assert file_response is not None
    assert rule_response is not None
    assert file_response.kind is NotifierResponseKind.APPROVE
    assert rule_response.kind is NotifierResponseKind.RULE_APPROVE
    # Same id string, different intent.
    assert file_response.decision_id == rule_response.decision_id == "abc"


async def test_callback_query_updates_yield_response() -> None:
    update_payload = {
        "result": [
            {
                "update_id": 1,
                "callback_query": {
                    "id": "cb1",
                    "data": "approve:abc",
                    "message": {"chat": {"id": 42}},
                },
            }
        ]
    }

    calls: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url.path))
        if request.url.path.endswith("/getUpdates"):
            if calls.count("/bott/getUpdates") == 1:
                return httpx.Response(200, json=update_payload)
            return httpx.Response(200, json={"result": []})
        if request.url.path.endswith("/answerCallbackQuery"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    transport = httpx.MockTransport(handle)
    inbound = TelegramInbound(
        bot_token="t",
        chat_id=42,
        poll_timeout_s=0.0,
        client=httpx.AsyncClient(transport=transport),
    )

    async for response in inbound.stream():
        assert response.kind is NotifierResponseKind.APPROVE
        assert response.decision_id == "abc"
        await inbound.stop()
        break

    assert any("/answerCallbackQuery" in c for c in calls)


async def test_reply_to_message_uses_marker_for_correlation() -> None:
    bot_text = f"Move CV.pdf?\nProposed: Foo\n\n{ID_MARKER_PREFIX}deadbeef"
    update_payload = {
        "result": [
            {
                "update_id": 2,
                "message": {
                    "chat": {"id": 42},
                    "text": "Career/CVs",
                    "reply_to_message": {"text": bot_text},
                },
            }
        ]
    }

    counter = {"n": 0}

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            counter["n"] += 1
            if counter["n"] == 1:
                return httpx.Response(200, json=update_payload)
            return httpx.Response(200, json={"result": []})
        return httpx.Response(404)

    transport = httpx.MockTransport(handle)
    inbound = TelegramInbound(
        bot_token="t",
        chat_id=42,
        poll_timeout_s=0.0,
        client=httpx.AsyncClient(transport=transport),
    )

    async for response in inbound.stream():
        assert response.kind is NotifierResponseKind.PROPOSE
        assert response.decision_id == "deadbeef"
        assert response.proposed_destination == Path("Career/CVs")
        await inbound.stop()
        break


async def test_messages_from_other_chats_are_ignored() -> None:
    update_payload = {
        "result": [
            {"update_id": 1, "message": {"chat": {"id": 999}, "text": "/approve abc"}},
            {"update_id": 2, "message": {"chat": {"id": 42}, "text": "/approve abc"}},
        ]
    }

    counter = {"n": 0}

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            counter["n"] += 1
            if counter["n"] == 1:
                return httpx.Response(200, json=update_payload)
            return httpx.Response(200, json={"result": []})
        return httpx.Response(404)

    transport = httpx.MockTransport(handle)
    inbound = TelegramInbound(
        bot_token="t",
        chat_id=42,
        poll_timeout_s=0.0,
        client=httpx.AsyncClient(transport=transport),
    )

    seen: list[Any] = []
    async for response in inbound.stream():
        seen.append(response)
        if len(seen) >= 1:
            await inbound.stop()
            break

    assert len(seen) == 1
    assert seen[0].decision_id == "abc"


def test_offset_save_and_load_round_trip(tmp_path: Path) -> None:
    """A new listener picks up the persisted cursor on construction."""
    offset_path = tmp_path / "telegram_offset.txt"

    first = TelegramInbound(
        bot_token="t",
        chat_id=42,
        poll_timeout_s=0.0,
        client=httpx.AsyncClient(),
        offset_path=offset_path,
    )
    first._save_offset(2049)
    assert first._load_offset() == 2049

    second = TelegramInbound(
        bot_token="t",
        chat_id=42,
        poll_timeout_s=0.0,
        client=httpx.AsyncClient(),
        offset_path=offset_path,
    )
    assert second._load_offset() == 2049


def test_offset_load_returns_zero_when_file_missing(tmp_path: Path) -> None:
    inbound = TelegramInbound(
        bot_token="t",
        chat_id=42,
        poll_timeout_s=0.0,
        client=httpx.AsyncClient(),
        offset_path=tmp_path / "missing.txt",
    )
    assert inbound._load_offset() == 0


def test_save_offset_silent_on_disk_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Persistence is best-effort; an OSError must not escape."""
    inbound = TelegramInbound(
        bot_token="t",
        chat_id=42,
        poll_timeout_s=0.0,
        client=httpx.AsyncClient(),
        offset_path=tmp_path / "telegram_offset.txt",
    )

    def raise_oserror(*args: object, **kwargs: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(Path, "write_text", raise_oserror)
    inbound._save_offset(7)


def test_offset_load_returns_zero_on_corrupt_file(tmp_path: Path) -> None:
    offset_path = tmp_path / "telegram_offset.txt"
    offset_path.write_text("not a number", encoding="utf-8")
    inbound = TelegramInbound(
        bot_token="t",
        chat_id=42,
        poll_timeout_s=0.0,
        client=httpx.AsyncClient(),
        offset_path=offset_path,
    )
    assert inbound._load_offset() == 0


async def test_stream_picks_up_persisted_offset(tmp_path: Path) -> None:
    """The first ``getUpdates`` request uses the persisted cursor.

    The new cursor is persisted once per batch (after the for-loop
    finishes), so the consumer must let the generator complete the
    batch rather than ``break`` mid-yield. A consumer that breaks
    immediately leaves the cursor at the previous value; the
    pending-log's ``APPLIED`` state machine then handles any replay
    on the next restart.
    """
    offset_path = tmp_path / "telegram_offset.txt"
    offset_path.write_text("4242", encoding="utf-8")

    captured: list[int] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        captured.append(int(request.url.params.get("offset", 0)))
        return httpx.Response(
            200,
            json={
                "result": [
                    {
                        "update_id": 9999,
                        "message": {
                            "chat": {"id": 42},
                            "text": "/approve abc",
                        },
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handle)
    inbound = TelegramInbound(
        bot_token="t",
        chat_id=42,
        poll_timeout_s=0.0,
        client=httpx.AsyncClient(transport=transport),
        offset_path=offset_path,
    )
    async for response in inbound.stream():
        assert response.decision_id == "abc"
        # Don't break: let the generator finish the batch's
        # for-loop so the post-loop ``_save_offset`` runs. ``stop``
        # arms the outer while to exit on its next check.
        await inbound.stop()

    assert captured[0] == 4242
    # New cursor is persisted as 9999 + 1.
    assert offset_path.read_text(encoding="utf-8").strip() == "10000"


async def test_unparseable_callback_is_acknowledged_anyway() -> None:
    """An unrecognised callback still gets answerCallbackQuery so the spinner clears."""
    payload = {
        "result": [
            {
                "update_id": 1,
                "callback_query": {
                    "id": "cb1",
                    "data": "junk",
                    "message": {"chat": {"id": 42}},
                },
            },
            {
                "update_id": 2,
                "callback_query": {
                    "id": "cb2",
                    "data": "approve:abc",
                    "message": {"chat": {"id": 42}},
                },
            },
        ]
    }

    answered: list[str] = []
    counter = {"n": 0}

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            counter["n"] += 1
            if counter["n"] == 1:
                return httpx.Response(200, json=payload)
            return httpx.Response(200, json={"result": []})
        if request.url.path.endswith("/answerCallbackQuery"):
            body = json.loads(request.content.decode("utf-8"))
            answered.append(body["callback_query_id"])
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    transport = httpx.MockTransport(handle)
    inbound = TelegramInbound(
        bot_token="t",
        chat_id=42,
        poll_timeout_s=0.0,
        client=httpx.AsyncClient(transport=transport),
    )

    async for response in inbound.stream():
        assert response.kind is NotifierResponseKind.APPROVE
        await inbound.stop()
        break

    assert "cb1" in answered
    assert "cb2" in answered
