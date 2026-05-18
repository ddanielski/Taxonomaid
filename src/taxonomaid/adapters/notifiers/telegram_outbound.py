"""Direct Telegram Bot API outbound adapter.

Sends pending-decision prompts and rule-review proposals as
inline-keyboard messages, so the user can approve / reject with a
single tap and supply a custom path by *replying* to the bot's
message - no copying long IDs around.

Used in preference to :class:`AppriseOutbound` when Telegram credentials
are configured; Apprise covers every other channel.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import httpx

from taxonomaid.adapters.notifiers.telegram_protocol import ID_MARKER_PREFIX
from taxonomaid.domain import NotifierError, Rule

_HTTP_ERROR_THRESHOLD: Final[int] = 400
_ERROR_BODY_PREVIEW_CHARS: Final[int] = 300
_DEFAULT_TIMEOUT_S: Final[float] = 30.0

# Telegram caps ``sendMessage.text`` at 4096 UTF-16 code units. The
# fixed scaffolding (filename, proposed destination, prompt, marker)
# eats roughly 600 chars worst-case; truncating reason to 3000 keeps
# the whole body well under the cap and out of the silent-400 path.
_MAX_REASON_CHARS: Final[int] = 3000


class TelegramOutbound:
    """Outbound notifier using the Telegram Bot API directly.

    Args:
        bot_token: The bot token issued by ``@BotFather``.
        chat_id: Target chat id (negative for groups / channels).
        client: Optional pre-configured async HTTP client. When ``None``,
            a default client is built lazily and closed on :meth:`aclose`.
    """

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._client = client
        self._owns_client = client is None

    async def aclose(self) -> None:
        """Close the owned HTTP client."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def notify_pending(
        self,
        *,
        decision_id: str,
        file: Path,
        proposed_destination: Path,
        confidence: float,
        reason: str,
    ) -> None:
        """Send a pending-decision prompt with inline approve / reject buttons.

        The message body ends with a ``#id:<decision_id>`` marker so the
        inbound listener can correlate any later free-form reply back to
        this decision via ``reply_to_message``.
        """
        text = _build_message(
            decision_id=decision_id,
            file=file,
            proposed_destination=proposed_destination,
            confidence=confidence,
            reason=reason,
        )
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"approve:{decision_id}"},
                    {"text": "❌ Reject", "callback_data": f"reject:{decision_id}"},
                ]
            ]
        }
        await self._send(text, reply_markup=keyboard)

    async def notify_rule_proposal(
        self,
        *,
        proposal: Rule,
        sample_filenames: tuple[str, ...] = (),
        index: int | None = None,
        total: int | None = None,
    ) -> None:
        """Send one rule-proposal prompt during a ``/review`` session.

        The message body ends with a ``#rule:<proposal_id>`` marker for
        symmetry with the per-file flow's ``#id:`` marker, though the
        review path uses inline-keyboard callbacks rather than
        threaded replies for its actions.

        Args:
            proposal: The proposed :class:`Rule`.
            sample_filenames: Up to ~5 example filenames the miner saw
                this rule predicate match. Surfaced verbatim in the
                message; long lists are truncated.
            index: Optional 1-based position in the queue. Pass with
                ``total`` for a "n of N" header.
            total: Optional total queue size. Pass with ``index``.
        """
        text = _build_proposal_message(
            proposal=proposal,
            sample_filenames=sample_filenames,
            index=index,
            total=total,
        )
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"rule_approve:{proposal.id}"},
                    {"text": "❌ Reject", "callback_data": f"rule_reject:{proposal.id}"},
                ]
            ]
        }
        await self._send(text, reply_markup=keyboard)

    async def notify_review_complete(self, *, approved: int, rejected: int) -> None:
        """Send the closing message when the review queue empties."""
        text = (
            "✅ Review complete.\n"
            f"Approved: {approved}\n"
            f"Rejected: {rejected}\n"
            "\n"
            "Run `taxonomaid mine` again next week to surface new "
            "proposals (or wait for the weekly timer)."
        )
        await self._send(text)

    async def notify_review_nudge(self, *, pending: int) -> None:
        """Send the post-mine 'you have N proposals' nudge.

        Distinct from :meth:`notify_review_complete` so the operator
        can tell push (post-mine) from pull (post-/review) at a
        glance.
        """
        text = (
            f"📐 You have {pending} new rule proposal{'s' if pending != 1 else ''} "
            "from the latest mine pass.\n"
            "\n"
            "Reply /review to walk through them on Telegram, or run "
            "`taxonomaid review` for the CLI experience (regex "
            "editing, full sample list)."
        )
        await self._send(text)

    async def notify_audit_findings(
        self,
        *,
        findings_by_kind: Mapping[str, tuple[str, ...]],
    ) -> None:
        """Send a digest of audit findings as a single message.

        Args:
            findings_by_kind: ``kind -> tuple[str, ...]`` where each
                string is one rendered finding line. The audit timer
                produces this from the :class:`AuditFinding` records
                so the wire-format here stays decoupled from the
                domain object's exact field layout.
        """
        if not findings_by_kind:
            return
        sections: list[str] = []
        for kind, lines in findings_by_kind.items():
            if not lines:
                continue
            header = f"  {kind} ({len(lines)})"
            body = "\n".join(f"    • {_truncate(_sanitise_display(line), 200)}" for line in lines)
            sections.append(f"{header}\n{body}")
        body = "\n\n".join(sections) if sections else "  (no findings)"
        text = (
            "🩺 Audit findings\n"
            "\n"
            f"{body}\n"
            "\n"
            "Run `taxonomaid audit` for the full report; check the\n"
            "decision log if anything looks off."
        )
        await self._send(_truncate(text, 4000))

    async def notify_circuit_open(self, *, reason: str) -> None:
        """Notify the operator that the LLM circuit just tripped open.

        Sent **once** when the consecutive-failure threshold is
        crossed. While the circuit is open, files are parked
        silently (no per-file notifier prompts) so a Gemini outage
        doesn't produce a 100-prompt notification storm.
        """
        text = (
            "🚨 LLM unavailable.\n"
            "\n"
            "Files arriving while the circuit is open will be parked\n"
            "silently in _unsorted/ - no per-file Telegram prompt -\n"
            "until I can reach the LLM again.\n"
            "\n"
            f"Trigger: {_truncate(_sanitise_display(reason), 300)}"
        )
        await self._send(text)

    async def notify_circuit_recovered(self, *, skipped_files: int) -> None:
        """Notify the operator that the LLM circuit just closed.

        Sent **once** on the transition. ``skipped_files`` reports
        how many files were parked silently while the circuit was
        open so the operator knows there's a backlog waiting in
        ``/review`` or ``_unsorted/``.
        """
        text = (
            "✅ LLM available again. Resuming classification.\n"
            "\n"
            f"{skipped_files} file{'s' if skipped_files != 1 else ''} "
            "parked silently while the circuit was open. Run\n"
            "`taxonomaid mine` and review them at your convenience\n"
            "(or check the new files in _unsorted/ directly)."
        )
        await self._send(text)

    async def _send(
        self,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        client = self._get_client()
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        try:
            resp = await client.post(url, json=payload, timeout=_DEFAULT_TIMEOUT_S)
        except httpx.HTTPError as exc:
            msg = f"telegram sendMessage failed: {exc}"
            raise NotifierError(msg) from exc

        if resp.status_code >= _HTTP_ERROR_THRESHOLD:
            preview = resp.text[:_ERROR_BODY_PREVIEW_CHARS]
            msg = f"telegram sendMessage returned HTTP {resp.status_code}: {preview}"
            raise NotifierError(msg)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(_DEFAULT_TIMEOUT_S),
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            )
        return self._client


def _build_message(
    *,
    decision_id: str,
    file: Path,
    proposed_destination: Path,
    confidence: float,
    reason: str,
) -> str:
    # ``file.name`` is attacker-controlled (a malicious filename can
    # include newlines on Linux). Sanitise before interpolation so a
    # planted ``report\n#id:deadbeef.pdf`` can't break out of the
    # first line and spoof the inbound ID marker - the inbound regex
    # (line-anchored, last-match-wins) is the authoritative defence,
    # but cleaner display is also UX.
    safe_filename = _sanitise_display(file.name)
    return (
        f"📁 Move {safe_filename}?\n"
        f"Proposed: {proposed_destination}\n"
        f"Confidence: {confidence:.2f}\n"
        f"Reason: {_truncate(reason, _MAX_REASON_CHARS)}\n"
        f"\n"
        f"Tap Approve / Reject, or reply to this message with a custom path.\n"
        f"\n"
        f"{ID_MARKER_PREFIX}{decision_id}"
    )


# Each sample filename is sanitised + length-capped so a hostile name
# can't break the message frame (same posture as ``_build_message``).
# The list is also bounded to a small maximum because Telegram caps
# ``sendMessage.text`` at 4096 UTF-16 code units.
_MAX_SAMPLE_FILENAMES_PER_PROPOSAL: Final[int] = 5
_MAX_SAMPLE_FILENAME_CHARS: Final[int] = 80
_RULE_MARKER_PREFIX: Final[str] = "#rule:"


def _build_proposal_message(
    *,
    proposal: Rule,
    sample_filenames: tuple[str, ...],
    index: int | None,
    total: int | None,
) -> str:
    """Render a rule proposal as a single Telegram message body."""
    title = "📐 Rule proposal"
    if index is not None and total is not None:
        title += f" {index} of {total}"

    pattern_lines: list[str] = []
    if proposal.match.filename_regex is not None:
        pattern_lines.append(f"  filename matches: {proposal.match.filename_regex}")
    if proposal.match.ext is not None:
        pattern_lines.append(f"  extension: {', '.join(proposal.match.ext)}")
    if proposal.match.mime_types is not None:
        pattern_lines.append(f"  mime: {', '.join(proposal.match.mime_types)}")
    if proposal.match.content_keywords is not None:
        pattern_lines.append(f"  content contains: {', '.join(proposal.match.content_keywords)}")
    if not pattern_lines:
        pattern_lines.append("  (no predicates - matches everything)")

    samples = sample_filenames[:_MAX_SAMPLE_FILENAMES_PER_PROPOSAL]
    if samples:
        sample_block = "\n".join(
            f"  {_truncate(_sanitise_display(s), _MAX_SAMPLE_FILENAME_CHARS)}" for s in samples
        )
    else:
        sample_block = "  (no samples recorded)"

    return (
        f"{title}\n"
        "\n"
        "Pattern:\n" + "\n".join(pattern_lines) + "\n"
        f"Destination: {proposal.destination_template}\n"
        f"Precision: {proposal.confidence:.0%}  ({proposal.sample_count} sample(s))\n"
        "\n"
        "Sample filenames:\n"
        f"{sample_block}\n"
        "\n"
        "Tap Approve to add to rules.yaml, Reject to add to rejected_rules.yaml.\n"
        "\n"
        f"{_RULE_MARKER_PREFIX}{proposal.id}"
    )


def _sanitise_display(text: str) -> str:
    """Collapse whitespace + strip control characters for safe rendering."""
    if not text:
        return ""
    scrubbed = "".join(ch if ch.isprintable() else " " for ch in text)
    return " ".join(scrubbed.split())


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"
