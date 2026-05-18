"""Structured logging configuration.

Initialised exactly once by :func:`taxonomaid.bootstrap.build_app`; never at
import time.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any, Final

import structlog
from structlog.types import EventDict, Processor

from taxonomaid.domain import ConfigError

# Patterns we scrub from every log payload. Two providers in active
# use today; new providers can be added here without touching the
# scrub plumbing.
#
# * Telegram bot tokens are ``<numeric_id>:<long-string>`` and the
#   API URLs prepend the literal ``bot`` - matches both raw tokens
#   and the typical leak vector (debug log of the request URL).
# * Google AI Studio / Gemini keys have the well-known
#   ``AIza`` prefix followed by 35 url-safe base64-ish characters.
#   We catch them anywhere they appear, including a 401 error body
#   echoed back by a misconfigured proxy.
_TELEGRAM_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"bot\d+:[A-Za-z0-9_\-]+")
_GEMINI_KEY_RE: Final[re.Pattern[str]] = re.compile(r"AIza[0-9A-Za-z_\-]{35}")
_REDACTED_TELEGRAM: Final[str] = "bot<redacted>"
_REDACTED_GEMINI: Final[str] = "AIza<redacted>"


def _scrub_string(text: str) -> str:
    """Apply every redactor pattern to ``text`` in order."""
    text = _TELEGRAM_TOKEN_RE.sub(_REDACTED_TELEGRAM, text)
    return _GEMINI_KEY_RE.sub(_REDACTED_GEMINI, text)


def _redact_secrets(_logger: object, _name: str, event_dict: EventDict) -> EventDict:
    """Scrub every known secret pattern from a structlog event dict.

    Runs late in the processor chain so it sees the final structured
    form (after ``add_log_level`` / timestamping) but before rendering.
    Stdlib ``logging`` records bypass structlog entirely - those go
    through :class:`_RedactingFilter`.
    """
    return {key: _scrub(value) for key, value in event_dict.items()}


class _RedactingFilter(logging.Filter):
    """Apply secret-redaction patterns to stdlib ``LogRecord`` payloads.

    Required for ``--log-level DEBUG`` runs: the noisy-third-party
    pin is lifted at debug, and httpx then logs full request URLs
    straight to the stderr handler - which never sees the structlog
    processor chain. The filter mutates the record in place; mypy
    doesn't love that but ``LogRecord`` semantics demand it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _scrub_string(record.msg)
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(_scrub_string(arg) if isinstance(arg, str) else arg for arg in args)
        return True


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return _scrub_string(value)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        scrubbed = [_scrub(v) for v in value]
        return tuple(scrubbed) if isinstance(value, tuple) else scrubbed
    return value


# Processors shared between JSON and console paths. ``format_exc_info``
# is conditionally appended below: the JSON renderer needs it to
# render tracebacks as strings, but ``ConsoleRenderer`` has its own
# native exception formatter and emits a structlog UserWarning if
# ``format_exc_info`` is already in the chain.
_DEFAULT_PROCESSORS: Final[list[Processor]] = [
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    structlog.processors.StackInfoRenderer(),
    _redact_secrets,
]

# Third-party loggers whose default INFO output is noisy and - in the case of
# httpx and httpcore - leaks the full request URL, including any embedded
# bot tokens. We pin them to WARNING unless the user explicitly opts into
# debug-level output.
_NOISY_THIRD_PARTY_LOGGERS: Final[tuple[str, ...]] = (
    "httpx",
    "httpcore",
    "urllib3",
    "watchfiles",
    "apprise",
    "asyncio",
)


_ACCEPTED_LEVELS: Final[tuple[str, ...]] = (
    "DEBUG",
    "INFO",
    "WARNING",
    "ERROR",
    "CRITICAL",
)


def configure_logging(*, json: bool = False, level: str = "INFO") -> None:
    """Initialise :mod:`structlog` and the stdlib logging bridge.

    Args:
        json: When ``True``, emit one JSON object per log record (intended
            for production / systemd / Docker). When ``False``, emit a
            colourful, dev-friendly console renderer.
        level: Root logging level name. Accepted values are
            :data:`_ACCEPTED_LEVELS`; a typo raises
            :class:`taxonomaid.domain.ConfigError` rather than a
            ``KeyError`` from deep inside structlog. We deliberately
            don't trust :func:`logging.getLevelNamesMapping` here: it
            returns *every* level the process has registered, which
            includes any custom levels structlog itself can't filter
            on (``TRACE``, etc).
    """
    level_upper = level.upper()
    if level_upper not in _ACCEPTED_LEVELS:
        msg = f"unknown log level {level!r}; expected one of: {', '.join(_ACCEPTED_LEVELS)}"
        raise ConfigError(msg)
    valid_levels = logging.getLevelNamesMapping()

    renderer: Processor
    pre_renderer: list[Processor]
    if json:
        renderer = structlog.processors.JSONRenderer()
        # JSON output needs ``format_exc_info`` to turn the
        # ``exc_info`` tuple into a serialisable string.
        pre_renderer = [*_DEFAULT_PROCESSORS, structlog.processors.format_exc_info]
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
        # ConsoleRenderer formats exceptions natively; including
        # ``format_exc_info`` here would trip structlog's "remove
        # format_exc_info from your processor chain" UserWarning,
        # which our ``filterwarnings = ["error"]`` pytest gate
        # escalates to a test failure on every ``log.exception``
        # call.
        pre_renderer = list(_DEFAULT_PROCESSORS)

    structlog.configure(
        processors=[*pre_renderer, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(valid_levels[level_upper]),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(_RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level_upper)

    # ``DEBUG`` is the only stdlib level under ``INFO`` that we
    # interpret as "untie noisy loggers"; ``NOTSET`` would let every
    # logger inherit the root level which is intentionally silent.
    if level_upper != "DEBUG":
        for name in _NOISY_THIRD_PARTY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
