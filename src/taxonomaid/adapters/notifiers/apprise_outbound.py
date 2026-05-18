"""Apprise-backed :class:`taxonomaid.ports.NotifierOutbound` adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

import apprise
import structlog
from apprise import NotifyFormat
from pydantic import SecretStr

from taxonomaid.domain import NotifierError

_log = structlog.get_logger("taxonomaid.notifier.apprise")


class AppriseOutbound:
    """Outbound notifier dispatching to one or more Apprise URLs.

    The Apprise library is synchronous; calls are wrapped in
    :func:`asyncio.to_thread` so the dispatcher's event loop is never
    blocked on a network round-trip.
    """

    def __init__(self, urls: Sequence[str | SecretStr]) -> None:
        # Apprise URLs embed credentials in their path (``tgram://``
        # bot tokens, ``slack://`` webhook tokens, ...). Keep them
        # wrapped in :class:`SecretStr` for the adapter's lifetime so
        # an accidental ``repr()`` never reveals the bare URL. The
        # :meth:`SecretStr.get_secret_value` call only happens inside
        # :func:`_build_channel`, when we hand the URL to Apprise.
        self._urls: tuple[SecretStr, ...] = tuple(
            url if isinstance(url, SecretStr) else SecretStr(url) for url in urls
        )
        # Build one ``Apprise`` per URL so :meth:`notify_pending` can
        # report success per channel rather than collapsing the whole
        # batch to a single boolean. ``apprise.Apprise.notify`` returns
        # ``False`` if *any* attached URL fails - which means a
        # transient Discord blip would tear down a Slack notification
        # that already succeeded.
        self._channels: tuple[tuple[str, apprise.Apprise], ...] = tuple(
            (_url_scheme(url.get_secret_value()), _build_channel(url.get_secret_value()))
            for url in self._urls
        )

    @property
    def url_count(self) -> int:
        """Number of configured Apprise URLs.

        The raw URLs are intentionally not exposed; they embed
        credentials and the only legitimate use of this adapter is via
        :meth:`notify_pending`. Inspect the count if you need to know
        whether outbound is wired.
        """
        return len(self._urls)

    async def notify_pending(
        self,
        *,
        decision_id: str,
        file: Path,
        proposed_destination: Path,
        confidence: float,
        reason: str,
    ) -> None:
        """Send a pending-decision prompt via every configured channel.

        Failures are reported per channel: as long as *at least one*
        channel delivered, the call is treated as successful and the
        offending channels are surfaced via :class:`structlog`.
        Raises :class:`NotifierError` only when *every* channel
        failed.
        """
        if not self._channels:
            return
        title = f"Move {file.name}?"
        body = (
            f"Proposed destination: {proposed_destination}\n"
            f"Confidence: {confidence:.2f}\n"
            f"Reason: {reason}\n\n"
            f"Reply with one of:\n"
            f"  /approve {decision_id}\n"
            f"  /reject {decision_id}\n"
            f"  /move {decision_id} relative/path/"
        )
        results = await asyncio.gather(
            *(
                asyncio.to_thread(
                    channel.notify,
                    body=body,
                    title=title,
                    body_format=NotifyFormat.TEXT,
                )
                for _, channel in self._channels
            ),
            return_exceptions=True,
        )
        failed_schemes: list[str] = []
        succeeded = 0
        for (scheme, _), outcome in zip(self._channels, results, strict=True):
            if isinstance(outcome, BaseException) or not outcome:
                failed_schemes.append(scheme)
                _log.warning(
                    "apprise_channel_failed",
                    decision_id=decision_id,
                    scheme=scheme,
                    error=str(outcome) if isinstance(outcome, BaseException) else "rejected",
                )
            else:
                succeeded += 1
        if succeeded == 0:
            msg = (
                f"apprise notify failed for decision_id={decision_id}; "
                f"all channels failed (schemes: {', '.join(sorted(set(failed_schemes)))})"
            )
            raise NotifierError(msg)


def _url_scheme(url: str) -> str:
    return url.split("://", 1)[0] if "://" in url else "<no-scheme>"


def _build_channel(url: str) -> apprise.Apprise:
    channel = apprise.Apprise()
    if not channel.add(url):
        scheme = _url_scheme(url)
        msg = f"apprise rejected URL with scheme {scheme!r}"
        raise NotifierError(msg)
    return channel
