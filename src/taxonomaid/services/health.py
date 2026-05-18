"""Liveness probes for ``taxonomaid health`` and the Docker HEALTHCHECK.

These probes are intentionally cheap so they're safe to run from a
five-minute Docker HEALTHCHECK without burning LLM credits or hitting
Telegram's rate limit. They check **reachability and authentication**,
not classification quality:

* :func:`probe_llm` issues a ``GET /models`` request against the LLM
  base URL with the configured ``Authorization`` header. Gemini and
  OpenAI both accept it; for endpoints that don't expose ``/models``
  (some self-hosted setups), a 404 is treated as "reachable but no
  catalogue" and reported as healthy.
* :func:`probe_telegram` calls ``GET /getMe`` once - it's the canonical
  auth-test endpoint and costs nothing in the rate-limit budget.

Both helpers raise no exceptions; they return a :class:`ProbeResult`
that the CLI / HEALTHCHECK turns into an exit code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import httpx

# Probe budget. Aligns with the Docker HEALTHCHECK ``timeout: 15s`` set
# in the Dockerfile minus a small margin so the CLI returns before the
# orchestrator declares the check itself timed out.
_DEFAULT_TIMEOUT_S: Final[float] = 10.0

_TELEGRAM_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"bot\d+:[A-Za-z0-9_\-]+")
_GEMINI_KEY_RE: Final[re.Pattern[str]] = re.compile(r"AIza[0-9A-Za-z_\-]{35}")
_REDACTED_TELEGRAM: Final[str] = "bot<redacted>"
_REDACTED_GEMINI: Final[str] = "AIza<redacted>"


def _redact(text: str) -> str:
    """Scrub known secret patterns from probe ``detail`` strings.

    ``httpx.HTTPError`` subclasses include the request URL in their
    string representation, which for Telegram contains the bot token
    and for some Gemini misconfigurations contains the API key. The
    CLI prints :class:`ProbeResult.detail` via Rich on stderr,
    bypassing the structlog redactor entirely - so the scrub has to
    happen here at construction time.
    """
    text = _TELEGRAM_TOKEN_RE.sub(_REDACTED_TELEGRAM, text)
    return _GEMINI_KEY_RE.sub(_REDACTED_GEMINI, text)


_HTTP_OK: Final[int] = 200
_HTTP_NOT_FOUND: Final[int] = 404
_HTTP_AUTH_FAILURE_CODES: Final[frozenset[int]] = frozenset({401, 403})
_HTTP_TELEGRAM_AUTH_FAILURE_CODES: Final[frozenset[int]] = frozenset({401, 403, 404})


class ProbeStatus(StrEnum):
    """Outcome of a single liveness probe."""

    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Result of probing one subsystem."""

    name: str
    status: ProbeStatus
    detail: str

    @property
    def is_healthy(self) -> bool:
        """``True`` for ``OK``; ``DEGRADED`` and ``FAILED`` count as unhealthy."""
        return self.status is ProbeStatus.OK


async def probe_llm(
    *,
    base_url: str,
    api_key: str,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    client: httpx.AsyncClient | None = None,
) -> ProbeResult:
    """Probe an OpenAI-compatible LLM endpoint via ``GET /models``.

    A 200 / 401 / 403 means the host is reachable; 401/403 simply
    indicates the endpoint is up but the key is wrong, which we still
    surface as ``DEGRADED`` so HEALTHCHECK fails. A 404 is treated as
    ``OK`` because some self-hosted endpoints don't expose
    ``/models``; reaching them at all is enough for our purposes.
    """
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient()
    try:
        try:
            resp = await client.get(url, headers=headers, timeout=timeout_s)
        except httpx.HTTPError as exc:
            return ProbeResult(
                name="llm",
                status=ProbeStatus.FAILED,
                detail=f"unreachable: {exc}",
            )
        if resp.status_code == _HTTP_OK:
            return ProbeResult(name="llm", status=ProbeStatus.OK, detail=str(resp.url))
        if resp.status_code == _HTTP_NOT_FOUND:
            return ProbeResult(
                name="llm",
                status=ProbeStatus.OK,
                detail="reachable; /models not exposed (self-hosted endpoint)",
            )
        if resp.status_code in _HTTP_AUTH_FAILURE_CODES:
            return ProbeResult(
                name="llm",
                status=ProbeStatus.DEGRADED,
                detail=f"reachable but unauthorized (HTTP {resp.status_code})",
            )
        return ProbeResult(
            name="llm",
            status=ProbeStatus.DEGRADED,
            detail=f"unexpected HTTP {resp.status_code}",
        )
    finally:
        if owns_client:
            await client.aclose()


async def probe_telegram(
    *,
    bot_token: str,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    client: httpx.AsyncClient | None = None,
) -> ProbeResult:
    """Probe the Telegram Bot API via ``getMe`` (the canonical auth test)."""
    url = f"https://api.telegram.org/bot{bot_token}/getMe"
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient()
    try:
        try:
            resp = await client.get(url, timeout=timeout_s)
        except httpx.HTTPError as exc:
            return ProbeResult(
                name="telegram",
                status=ProbeStatus.FAILED,
                detail=_redact(f"unreachable: {exc}"),
            )
        try:
            payload = resp.json()
        except ValueError:
            # A CDN intermediate or captive portal can return HTML with
            # status 200; treat that as ``DEGRADED`` rather than crashing
            # the health command (which would mask the real failure).
            return ProbeResult(
                name="telegram",
                status=ProbeStatus.DEGRADED,
                detail=f"non-JSON response (HTTP {resp.status_code}); intermediate?",
            )
        if resp.status_code == _HTTP_OK and isinstance(payload, dict) and payload.get("ok") is True:
            return ProbeResult(
                name="telegram",
                status=ProbeStatus.OK,
                detail="bot reachable",
            )
        if resp.status_code in _HTTP_TELEGRAM_AUTH_FAILURE_CODES:
            return ProbeResult(
                name="telegram",
                status=ProbeStatus.DEGRADED,
                detail=f"unauthorized or unknown bot (HTTP {resp.status_code})",
            )
        return ProbeResult(
            name="telegram",
            status=ProbeStatus.DEGRADED,
            detail=f"unexpected HTTP {resp.status_code}",
        )
    finally:
        if owns_client:
            await client.aclose()


async def probe_telegram_chat_is_private(
    *,
    bot_token: str,
    chat_id: int,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    client: httpx.AsyncClient | None = None,
) -> ProbeResult:
    """Probe ``getChat`` and warn when ``chat_id`` is a group/channel.

    The dispatcher's inbound listener only filters on ``chat_id``;
    if the configured chat is a group, every group member can press
    the inline-keyboard approve / reject buttons and act as the
    operator. Most users intend a private chat with their own bot;
    this probe surfaces the misconfiguration via a one-line
    ``DEGRADED`` advisory at ``taxonomaid health`` time.
    """
    url = f"https://api.telegram.org/bot{bot_token}/getChat"
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient()
    try:
        try:
            resp = await client.get(url, params={"chat_id": chat_id}, timeout=timeout_s)
        except httpx.HTTPError as exc:
            return ProbeResult(
                name="telegram-chat-type",
                status=ProbeStatus.FAILED,
                detail=_redact(f"unreachable: {exc}"),
            )
        try:
            payload = resp.json()
        except ValueError:
            return ProbeResult(
                name="telegram-chat-type",
                status=ProbeStatus.DEGRADED,
                detail=f"non-JSON response (HTTP {resp.status_code})",
            )
        if (
            resp.status_code != _HTTP_OK
            or not isinstance(payload, dict)
            or payload.get("ok") is not True
        ):
            return ProbeResult(
                name="telegram-chat-type",
                status=ProbeStatus.DEGRADED,
                detail=f"getChat returned HTTP {resp.status_code}",
            )
        result = payload.get("result", {})
        chat_type = result.get("type", "<unknown>") if isinstance(result, dict) else "<unknown>"
        if chat_type == "private":
            return ProbeResult(
                name="telegram-chat-type",
                status=ProbeStatus.OK,
                detail="private chat (recommended)",
            )
        return ProbeResult(
            name="telegram-chat-type",
            status=ProbeStatus.DEGRADED,
            detail=(
                f"chat type is {chat_type!r}: every member can approve / reject "
                "moves. Configure a private 1:1 chat with the bot unless this is "
                "intentional."
            ),
        )
    finally:
        if owns_client:
            await client.aclose()
