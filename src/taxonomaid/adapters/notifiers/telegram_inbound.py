"""Telegram inbound notifier.

Long-polls the Telegram Bot API's ``getUpdates`` endpoint over plain
HTTPS; no public URL or webhook needed (NAS-friendly).

Three reply mechanisms are recognised, in order of UX preference:

1. **Inline keyboard buttons** (sent by :class:`TelegramOutbound`):
   ``approve:<decision_id>`` / ``reject:<decision_id>`` callback data.
2. **Reply to the bot's message** with a custom path. The original bot
   message ends with a ``#id:<decision_id>`` marker that the listener
   parses out of ``reply_to_message.text`` to correlate the decision.
3. **Free-form commands** for power users::

       /approve <decision_id>
       /reject  <decision_id>
       /move    <decision_id> <relative/path/>
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import unicodedata
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Final

import httpx

from taxonomaid.adapters.notifiers.telegram_protocol import ID_MARKER_PREFIX
from taxonomaid.domain import NotifierError
from taxonomaid.ports import NotifierResponse, NotifierResponseKind

_HTTP_ERROR_THRESHOLD: Final[int] = 400
_ERROR_BODY_PREVIEW_CHARS: Final[int] = 300
_APPROVE_REJECT_PARTS: Final[int] = 2
_MOVE_PARTS: Final[int] = 3

# Most filesystems cap a full path at 4096 bytes (Linux PATH_MAX);
# rejecting longer proposals here lets us return a friendly error
# rather than letting the move attempt fail with ENAMETOOLONG deep
# inside the dispatcher.
_MAX_PROPOSED_PATH_CHARS: Final[int] = 4096

# The ``#id:`` marker is fenced to its own line at the end of the
# bot's outbound message (see :class:`TelegramOutbound`). Anchoring
# the regex to a line boundary with a length bound prevents a
# malicious filename like ``report_#id:dead00.pdf`` from spoofing the
# marker: a substring match in the body of the message would
# otherwise win because ``re.search`` returns the first match.
# Hex correlator length is bounded by :func:`_new_decision_id`'s
# 26-char truncation; we allow 1-32 hex digits for headroom.
_ID_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    rf"(?m)^{re.escape(ID_MARKER_PREFIX)}([0-9a-fA-F]{{1,32}})\s*$"
)

# Path-segment grammar accepted from Telegram replies. Deliberately
# conservative: a friendly chat message ("ok thanks") should be ignored,
# not turned into a folder named "ok thanks/". A free-form reply MUST
# contain at least one ``/`` so single-word confirmations like "ok",
# "yes", or "no." can never be misread as folder names. Single-name
# placements remain available via the explicit ``/move <id> <name>``
# command. ``\w`` is Unicode-aware by default in Python regex, so
# non-ASCII folder names work too: ``Documenti/Casa``,
# ``経理/2025/領収書``, ``Acta/2025``. The dispatcher's
# :func:`safe_resolve` is still the actual security boundary against
# ``..`` traversal.
_PATH_SEGMENT_RE: Final[re.Pattern[str]] = re.compile(r"[\w\-. ]+", re.UNICODE)

# Reject zero-width / format / bidi characters: they render invisibly
# but change how a path matches against another visually identical
# one. ``Cf`` is "Format" (zero-width joiner, RTL/LTR marks),
# ``Cc`` is "Control".
_INVISIBLE_CATEGORIES: Final[frozenset[str]] = frozenset({"Cf", "Cc"})


def _has_invisible_chars(text: str) -> bool:
    return any(unicodedata.category(ch) in _INVISIBLE_CATEGORIES for ch in text)


def looks_like_relative_path(text: str) -> bool:
    """Return ``True`` when ``text`` plausibly names a directory.

    Requires at least one ``/`` separator. The dispatcher's
    :func:`safe_resolve` is the actual security boundary against
    ``..`` traversal; this predicate just keeps "ok thanks" from
    being interpreted as a folder name.

    Inputs are NFC-normalised before pattern matching so two visually
    identical paths produced by different keyboards (e.g. ``é`` as a
    single codepoint vs. ``e`` + combining acute) collapse to the
    same string. Any zero-width / format / bidi characters cause an
    immediate reject - they're invisible in the chat UI and hostile
    in a path.
    """
    if not text:
        return False
    if _has_invisible_chars(text):
        return False
    normalised = unicodedata.normalize("NFC", text)
    stripped = normalised.strip()
    if not stripped or stripped.startswith("/"):
        return False
    if ".." in stripped:
        return False
    if "/" not in stripped:
        return False
    parts = stripped.strip("/").split("/")
    if any(p in {"", "."} for p in parts):
        return False
    return all(_PATH_SEGMENT_RE.fullmatch(p) is not None for p in parts)


class TelegramInbound:
    """Telegram :class:`taxonomaid.ports.NotifierInbound` adapter."""

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: int,
        poll_timeout_s: float = 30.0,
        client: httpx.AsyncClient | None = None,
        offset_path: Path | None = None,
    ) -> None:
        """Construct the listener.

        Args:
            bot_token: Token from ``@BotFather``.
            chat_id: The single chat the listener filters on.
            poll_timeout_s: Long-poll timeout.
            client: Optional pre-configured async client (tests inject
                one with a ``MockTransport``).
            offset_path: Where to persist the ``getUpdates`` cursor
                across restarts. The Bot API retains updates for ~24
                hours; without persistence, a daemon restart inside
                that window replays already-handled messages, which can
                cause stale ``/move`` commands to retry-move a file
                that's no longer parked. ``None`` disables persistence
                (intended for tests; production wiring always sets it).
        """
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._poll_timeout_s = poll_timeout_s
        self._client = client
        self._owns_client = client is None
        self._offset_path = offset_path
        self._stop = asyncio.Event()

    @property
    def chat_id(self) -> int:
        """The configured chat ID."""
        return self._chat_id

    async def stream(self) -> AsyncIterator[NotifierResponse]:
        """Yield user replies as they arrive.

        On HTTP error, the generator raises :class:`NotifierError`
        rather than retrying internally. The dispatcher's
        ``_inbound_loop`` is the reconnection authority: it catches
        :class:`NotifierError`, applies exponential backoff, and calls
        ``stream()`` again on the same instance. ``stream()`` is
        therefore reentrant **as long as** :meth:`stop` was not
        called - the offset is reloaded from disk on each call, the
        retained ``_client`` is reused, and the new generator picks
        up where the old one left off.

        Once :meth:`stop` is invoked, the instance is single-shot:
        the ``_stop`` event is sticky and a subsequent ``stream()``
        call returns immediately. Build a fresh adapter to resume.
        """
        client = self._get_client()
        offset = await asyncio.to_thread(self._load_offset)
        url = f"https://api.telegram.org/bot{self._bot_token}/getUpdates"
        while not self._stop.is_set():
            try:
                resp = await client.get(
                    url,
                    params={"timeout": int(self._poll_timeout_s), "offset": offset},
                    timeout=self._poll_timeout_s + 5,
                )
            except httpx.HTTPError as exc:
                msg = f"Telegram getUpdates failed: {exc}"
                raise NotifierError(msg) from exc

            if resp.status_code >= _HTTP_ERROR_THRESHOLD:
                preview = resp.text[:_ERROR_BODY_PREVIEW_CHARS]
                msg = f"Telegram getUpdates returned HTTP {resp.status_code}: {preview}"
                raise NotifierError(msg)

            results = resp.json().get("result", [])
            for update in results:
                update_id = int(update["update_id"])
                # Defensive monotonicity guard. Real Telegram getUpdates
                # responses are always in ascending update_id order, but
                # a reordered or replayed batch (e.g. a man-in-the-middle
                # test harness, a buggy reverse proxy) could otherwise
                # rewind the cursor and replay handled messages.
                if update_id < offset:
                    continue
                offset = update_id + 1
                response = await self._handle_update(update, client)
                if response is not None:
                    yield response
            # One save per batch is enough. If the consumer breaks
            # mid-batch the loop exits and the cursor stays where it
            # was: on restart we replay the last batch from disk and
            # ``PendingState.APPLIED`` short-circuits already-handled
            # decisions inside the dispatcher. Persisting per-update
            # would double the write volume on chatty bots for no
            # correctness gain.
            if results:
                await asyncio.to_thread(self._save_offset, offset)

    async def stop(self) -> None:
        """Signal the polling loop to exit and close the owned client."""
        self._stop.set()
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # Per-request timeouts in ``stream()`` already bound the
            # long-poll; the baseline here covers any future call site
            # (``answerCallbackQuery``, ``sendMessage``) that forgets
            # to set ``timeout=`` explicitly. The connection cap stops
            # accidental fan-out under retry storms.
            connect_timeout = self._poll_timeout_s + 5
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect_timeout),
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            )
        return self._client

    def _load_offset(self) -> int:
        """Restore the last persisted ``getUpdates`` offset, or zero."""
        if self._offset_path is None or not self._offset_path.is_file():
            return 0
        try:
            return int(self._offset_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return 0

    def _save_offset(self, offset: int) -> None:
        """Persist the cursor atomically; failures are silent (best-effort)."""
        if self._offset_path is None:
            return
        try:
            self._offset_path.parent.mkdir(parents=True, exist_ok=True)
            # ``Path.with_suffix`` would replace only the trailing
            # suffix (``offset.txt`` would become ``offset.tmp``,
            # losing the marker). Concatenating the full name with
            # ``.tmp`` is unambiguous: any extension is preserved.
            tmp = self._offset_path.parent / (self._offset_path.name + ".tmp")
            tmp.write_text(str(offset), encoding="utf-8")
            tmp.replace(self._offset_path)
        except OSError:
            # Persistence is an optimisation, not a correctness invariant -
            # at worst we replay updates after a crash, which the
            # PendingState.APPLIED short-circuit already handles.
            pass

    async def _handle_update(
        self,
        update: dict[str, Any],
        client: httpx.AsyncClient,
    ) -> NotifierResponse | None:
        if "callback_query" in update:
            cq = update["callback_query"]
            response = self._parse_callback(cq)
            await self._answer_callback(cq, client)
            return response
        if "message" in update:
            return self._parse_message(update["message"])
        return None

    def _parse_callback(self, cq: dict[str, Any]) -> NotifierResponse | None:
        message = cq.get("message", {})
        if message.get("chat", {}).get("id") != self._chat_id:
            return None
        return parse_callback_data(str(cq.get("data", "")))

    async def _answer_callback(
        self,
        cq: dict[str, Any],
        client: httpx.AsyncClient,
    ) -> None:
        # Best-effort acknowledgement so Telegram clears the loading
        # spinner. Failures here are logged elsewhere (the dispatcher's
        # outer try/except) and never block reply delivery.
        callback_id = cq.get("id")
        if not callback_id:
            return
        url = f"https://api.telegram.org/bot{self._bot_token}/answerCallbackQuery"
        with contextlib.suppress(httpx.HTTPError):
            await client.post(url, json={"callback_query_id": callback_id}, timeout=10.0)

    def _parse_message(self, message: dict[str, Any]) -> NotifierResponse | None:
        if message.get("chat", {}).get("id") != self._chat_id:
            return None
        text = str(message.get("text", "")).strip()

        reply_to = message.get("reply_to_message")
        if (
            isinstance(reply_to, dict)
            and text
            and len(text) <= _MAX_PROPOSED_PATH_CHARS
            and looks_like_relative_path(text)
        ):
            decision_id = extract_decision_id(str(reply_to.get("text", "")))
            if decision_id is not None:
                # ``looks_like_relative_path`` already rejected
                # invisible characters; NFC-normalise here so the
                # path stored in ``proposed_destination`` matches
                # whatever the dispatcher will produce when comparing
                # it against the destination tree on disk.
                normalised = unicodedata.normalize("NFC", text).strip()
                return NotifierResponse(
                    decision_id=decision_id,
                    kind=NotifierResponseKind.PROPOSE,
                    proposed_destination=Path(normalised),
                    raw_text=text,
                )

        return parse_reply(text)


def parse_callback_data(data: str) -> NotifierResponse | None:
    """Decode an inline-keyboard ``callback_data`` string.

    Recognised actions:

    * ``approve:<decision_id>`` / ``reject:<decision_id>`` -
      per-file move decisions emitted by
      :class:`TelegramOutbound.notify_pending`.
    * ``rule_approve:<proposal_id>`` / ``rule_reject:<proposal_id>`` -
      rule-proposal decisions emitted by
      :meth:`TelegramOutbound.notify_rule_proposal` during a
      ``/review`` session.

    Returns ``None`` for unrecognised or malformed inputs so the
    listener can ignore them without raising.
    """
    if ":" not in data:
        return None
    action, _, decision_id = data.partition(":")
    if not decision_id:
        return None
    if action == "approve":
        return NotifierResponse(
            decision_id=decision_id,
            kind=NotifierResponseKind.APPROVE,
            raw_text=data,
        )
    if action == "reject":
        return NotifierResponse(
            decision_id=decision_id,
            kind=NotifierResponseKind.REJECT,
            raw_text=data,
        )
    if action == "rule_approve":
        return NotifierResponse(
            decision_id=decision_id,
            kind=NotifierResponseKind.RULE_APPROVE,
            raw_text=data,
        )
    if action == "rule_reject":
        return NotifierResponse(
            decision_id=decision_id,
            kind=NotifierResponseKind.RULE_REJECT,
            raw_text=data,
        )
    return None


def extract_decision_id(text: str) -> str | None:
    """Extract the ``#id:<decision_id>`` marker from ``text``.

    The marker is anchored to a line boundary (see ``_ID_MARKER_RE``)
    and we return the **last** match if more than one is found.
    Together these mitigate a filename like ``report_#id:dead00.pdf``
    spoofing the marker: a marker embedded in the body of the
    outbound message no longer wins over the legitimate trailing
    marker on its own line.
    """
    matches = _ID_MARKER_RE.findall(text)
    return matches[-1] if matches else None


def parse_reply(text: str) -> NotifierResponse | None:
    """Parse a free-form Telegram command reply into a :class:`NotifierResponse`.

    Returns ``None`` when the message doesn't match the documented
    command grammar; the caller should ignore such messages.
    """
    if not text:
        return None
    parts = text.split(maxsplit=2)
    if not parts or not parts[0].startswith("/"):
        return None
    command = parts[0].lower()
    if command == "/approve" and len(parts) >= _APPROVE_REJECT_PARTS:
        return NotifierResponse(
            decision_id=parts[1],
            kind=NotifierResponseKind.APPROVE,
            raw_text=text,
        )
    if command == "/reject" and len(parts) >= _APPROVE_REJECT_PARTS:
        return NotifierResponse(
            decision_id=parts[1],
            kind=NotifierResponseKind.REJECT,
            raw_text=text,
        )
    if command == "/move" and len(parts) >= _MOVE_PARTS:
        proposed = parts[2]
        if len(proposed) > _MAX_PROPOSED_PATH_CHARS:
            # Refuse before allocating Path objects or hitting the
            # filesystem with an overlong path.
            return None
        return NotifierResponse(
            decision_id=parts[1],
            kind=NotifierResponseKind.PROPOSE,
            proposed_destination=Path(proposed),
            raw_text=text,
        )
    if command == "/review":
        # ``/review`` has no arguments. Empty ``decision_id`` is the
        # convention for review-control commands (the dispatcher
        # dispatches on ``kind``); any extra tokens after the
        # command are tolerated and ignored so the user can type
        # ``/review please`` without confusing the parser.
        return NotifierResponse(
            decision_id="",
            kind=NotifierResponseKind.REVIEW_START,
            raw_text=text,
        )
    return None
