"""Composition root.

This is the **only** module allowed to import both
:mod:`taxonomaid.ports` *and* :mod:`taxonomaid.adapters` *and*
:mod:`taxonomaid.services`. It builds wired services from a validated
:class:`AppConfig`; everything else stays decoupled.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import structlog
from pydantic import SecretStr

from taxonomaid.adapters.clock import SystemClock
from taxonomaid.adapters.decision_log import JsonlDecisionLog
from taxonomaid.adapters.filesystem import LocalFilesystem
from taxonomaid.adapters.llm import OpenAICompatProvider
from taxonomaid.adapters.notifiers import (
    AppriseOutbound,
    CompositeOutbound,
    TelegramInbound,
    TelegramOutbound,
)
from taxonomaid.adapters.pending_log import JsonlPendingLog
from taxonomaid.adapters.watcher import LocalWatcher
from taxonomaid.config import AppConfig, load_app_config, load_rules_file
from taxonomaid.domain import ConfigError, NotifierError, Rule
from taxonomaid.logging import configure_logging
from taxonomaid.ports import NotifierOutbound
from taxonomaid.services.circuit_breaker import LLMCircuit
from taxonomaid.services.dispatcher import Dispatcher, DispatcherDeps
from taxonomaid.services.review_session import ReviewPaths, ReviewSession
from taxonomaid.services.rule_engine import RuleEngine
from taxonomaid.services.similarity import SimilarityIndex

_log = structlog.get_logger("taxonomaid.bootstrap")


@dataclass(frozen=True, slots=True)
class App:
    """Top-level wired application."""

    config: AppConfig
    dispatcher: Dispatcher

    async def aclose(self) -> None:
        """Close every adapter that owns external resources.

        Walks the dispatcher's deps and calls ``aclose()`` on anything
        that exposes one (the OpenAI-compat HTTP client, the Telegram
        outbound HTTP client, the Telegram inbound long-poll loop).
        Errors are logged and swallowed - we're shutting down, so the
        right behaviour is "tear everything else down anyway".
        """
        deps = self.dispatcher.deps
        for resource in (
            deps.llm,
            deps.notifier_outbound,
            deps.notifier_inbound,
        ):
            if resource is None:
                continue
            close = getattr(resource, "aclose", None)
            if close is None:
                continue
            try:
                await close()
            except Exception as exc:
                _log.warning(
                    "aclose_error",
                    resource=type(resource).__name__,
                    error=str(exc),
                )


def build_app(
    config: AppConfig,
    *,
    config_dir: Path | None = None,
    log_json: bool = False,
    log_level: str = "INFO",
) -> App:
    """Wire concrete adapters into services from validated config.

    Args:
        config: Validated aggregated config.
        config_dir: Directory housing the YAML files. When supplied,
            the dispatcher gains a :class:`ReviewSession` so the
            Telegram inbound's ``/review`` command works; when
            omitted, ``/review`` is gracefully degraded (logged and
            ignored). The :func:`build_app_from_paths` overload
            populates this from ``watches_path.parent`` so the
            common CLI path always has it.
        log_json: When ``True``, emit JSON logs (production / systemd /
            Docker).
        log_level: Root logging level name.

    Returns:
        A fully wired :class:`App`.
    """
    configure_logging(json=log_json, level=log_level)

    filesystem = LocalFilesystem()
    clock = SystemClock()
    decision_log = JsonlDecisionLog(config.data_dir / "decisions.jsonl")
    pending_log = JsonlPendingLog(config.data_dir / "pending_decisions.jsonl")

    llm = OpenAICompatProvider(
        base_url=str(config.llm.base_url),
        model=config.llm.model,
        # Pass the SecretStr through so the provider keeps it wrapped
        # for its entire lifetime. The bare value never lives in a
        # plain attribute that a debugger or stray repr() can leak.
        api_key=config.llm.api_key,
        request_timeout_s=config.llm.request_timeout_s,
        max_excerpt_chars=config.llm.max_excerpt_chars,
    )

    telegram = config.notifier.telegram
    notifier_outbound: NotifierOutbound | None = None
    # Keep the Apprise URLs wrapped through bootstrap; AppriseOutbound
    # itself stores them as SecretStr and only unwraps at the
    # ``apprise.Apprise.add`` call site. That's two fewer places where
    # a bot token can land in a stray ``repr()``.
    apprise_urls = config.notifier.apprise_urls
    if telegram is not None:
        # Direct Bot API gives us inline-keyboard buttons + reply
        # correlation that Apprise's generic Telegram backend can't.
        telegram_outbound = TelegramOutbound(
            bot_token=telegram.bot_token.get_secret_value(),
            chat_id=telegram.chat_id,
        )
        # Any non-``tgram://`` URLs go through Apprise alongside the
        # direct Telegram outbound via a composite. A duplicate
        # ``tgram://`` URL is silently dropped so the user doesn't
        # get the same prompt twice. The ``startswith`` check requires
        # unwrapping each ``SecretStr``; we do it inside the
        # comprehension so the plaintext only lives for one iteration.
        non_telegram_urls = tuple(
            u for u in apprise_urls if not u.get_secret_value().lower().startswith("tgram://")
        )
        if non_telegram_urls:
            try:
                apprise_secondary = _build_apprise_outbound(non_telegram_urls)
            except ConfigError:
                # _build_apprise_outbound already raised with a
                # redacted message; re-raise so the CLI exits with
                # its usual config-error UX.
                raise
            notifier_outbound = CompositeOutbound([telegram_outbound, apprise_secondary])
        else:
            notifier_outbound = telegram_outbound
    elif apprise_urls:
        notifier_outbound = _build_apprise_outbound(apprise_urls)

    notifier_inbound: TelegramInbound | None = None
    if telegram is not None:
        notifier_inbound = TelegramInbound(
            bot_token=telegram.bot_token.get_secret_value(),
            chat_id=telegram.chat_id,
            poll_timeout_s=config.notifier.poll_timeout_s,
            offset_path=config.data_dir / "telegram_offset.txt",
        )

    engines_by_root = _load_rule_engines(config)
    rule_engine = RuleEngine(rules=())  # fallback for events outside the map
    watcher = LocalWatcher(config.watches.watches)
    similarity = SimilarityIndex()

    # The review session needs a config_dir to know where the three
    # YAML files live. When ``build_app`` is called directly with a
    # pre-loaded ``AppConfig`` (no path context), reviewing via
    # Telegram is unavailable - the dispatcher will log
    # ``review_response_dropped`` for any ``/review`` command. The
    # ``build_app_from_paths`` overload threads the directory through
    # so the typical CLI path Just Works.
    review_session: ReviewSession | None = None
    if config_dir is not None:
        review_session = ReviewSession(ReviewPaths.under(config_dir))

    dispatcher = Dispatcher(
        DispatcherDeps(
            config=config,
            rule_engine=rule_engine,
            llm=llm,
            notifier_outbound=notifier_outbound,
            notifier_inbound=notifier_inbound,
            filesystem=filesystem,
            decision_log=decision_log,
            pending_log=pending_log,
            watcher=watcher,
            clock=clock,
            similarity=similarity,
            rule_engines_by_root=engines_by_root,
            review_session=review_session,
            llm_circuit=LLMCircuit(),
        )
    )

    return App(config=config, dispatcher=dispatcher)


def build_app_from_paths(
    *,
    watches_path: Path,
    llm_path: Path,
    notifier_path: Path,
    data_dir: Path | None = None,
    log_json: bool = False,
    log_level: str = "INFO",
) -> App:
    """Convenience overload: load YAML, validate, then wire."""
    config = load_app_config(
        watches_path=watches_path,
        llm_path=llm_path,
        notifier_path=notifier_path,
        data_dir=data_dir,
    )
    return build_app(
        config,
        config_dir=watches_path.resolve().parent,
        log_json=log_json,
        log_level=log_level,
    )


def _build_apprise_outbound(urls: Sequence[SecretStr]) -> AppriseOutbound:
    """Construct :class:`AppriseOutbound` and translate config errors.

    :class:`AppriseOutbound` raises :class:`NotifierError` from its
    constructor when Apprise rejects a URL. That's a *configuration*
    failure - the daemon can't start - and the CLI already handles
    :class:`ConfigError` by printing a friendly message and exiting
    with code 1. Re-raise so the operator sees the same UX as for
    any other config-file mistake instead of an unhandled
    ``NotifierError`` traceback. The original error message is
    already redacted to a scheme only, so it's safe to surface.
    """
    try:
        return AppriseOutbound(urls)
    except NotifierError as exc:
        raise ConfigError(str(exc)) from exc


def _load_rule_engines(config: AppConfig) -> dict[Path, RuleEngine]:
    """Build a per-watch :class:`RuleEngine` map.

    Each watch gets its own engine constructed from its own
    ``rules_file``. Missing files are logged and skipped, not fatal:
    rules accumulate as the system learns, so ``rules.yaml`` legitimately
    won't exist on a fresh install. A rule written for ``~/Documents``
    will never fire on ``~/Photos``.
    """
    engines: dict[Path, RuleEngine] = {}
    for watch in config.watches.watches:
        rules: tuple[Rule, ...]
        if watch.rules_file is None:
            rules = ()
        elif not watch.rules_file.exists():
            _log.warning(
                "rules_file_missing",
                watch=str(watch.path),
                rules_file=str(watch.rules_file),
            )
            rules = ()
        else:
            rules = load_rules_file(watch.rules_file)
        engines[watch.path] = RuleEngine(rules=rules)
    return engines
