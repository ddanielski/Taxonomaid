"""Unit tests for :mod:`taxonomaid.logging`."""

from __future__ import annotations

import logging

import pytest

from taxonomaid.domain import ConfigError
from taxonomaid.logging import _redact_secrets, _scrub_string, configure_logging

pytestmark = pytest.mark.unit


def test_configure_logging_console() -> None:
    configure_logging(json=False, level="DEBUG")
    assert logging.getLogger().level == logging.DEBUG


def test_configure_logging_json() -> None:
    configure_logging(json=True, level="INFO")
    assert logging.getLogger().level == logging.INFO


def test_configure_logging_rejects_unknown_level() -> None:
    """H5 regression: a typo'd ``--log-level`` raises ConfigError, not KeyError."""
    with pytest.raises(ConfigError, match="unknown log level"):
        configure_logging(json=False, level="INFFO")
    with pytest.raises(ConfigError, match="unknown log level"):
        configure_logging(json=False, level="TRACE")


@pytest.mark.parametrize("noisy", ["httpx", "httpcore", "watchfiles", "apprise"])
def test_noisy_third_party_loggers_are_pinned_to_warning_at_info(noisy: str) -> None:
    logging.getLogger(noisy).setLevel(logging.NOTSET)
    configure_logging(json=False, level="INFO")
    assert logging.getLogger(noisy).level == logging.WARNING


def test_debug_level_does_not_pin_third_party_loggers() -> None:
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    configure_logging(json=False, level="DEBUG")
    assert logging.getLogger("httpx").level == logging.NOTSET


def test_stdlib_handler_redacts_bot_tokens(capsys: pytest.CaptureFixture[str]) -> None:
    """Stdlib loggers (httpx, watchfiles, …) must also be scrubbed.

    At ``--log-level DEBUG`` the third-party WARNING pin is lifted and
    httpx logs the full request URL. Without the stdlib filter the
    bot token would land on stderr.
    """
    configure_logging(json=False, level="DEBUG")
    httpx_logger = logging.getLogger("httpx")
    httpx_logger.setLevel(logging.DEBUG)
    httpx_logger.info(
        "HTTP Request: GET https://api.telegram.org/bot1234567890:AAH-real-secret/getUpdates"
    )
    captured = capsys.readouterr().err
    assert "AAH-real-secret" not in captured
    assert "bot<redacted>" in captured


def test_stdlib_handler_redacts_bot_tokens_in_args(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(json=False, level="DEBUG")
    logger = logging.getLogger("httpx-args-test")
    logger.setLevel(logging.DEBUG)
    logger.info("token=%s status=%d", "bot999:another-token", 200)
    captured = capsys.readouterr().err
    assert "another-token" not in captured


def test_redaction_strips_telegram_bot_tokens() -> None:
    payload = {
        "url": "https://api.telegram.org/bot1234567890:AAH-real-secret/getUpdates",
        "nested": {"again": "bot999:another-token here"},
        "harmless": "no token in this string",
        "list": ["bot12:abc", "ok"],
    }
    redacted = _redact_secrets(None, "info", dict(payload))
    assert "AAH-real-secret" not in str(redacted)
    assert "another-token" not in str(redacted)
    assert "abc" not in str(redacted["list"])
    assert redacted["harmless"] == "no token in this string"


def test_redaction_strips_gemini_api_keys() -> None:
    """Security review 3.2: Gemini ``AIza...`` keys are scrubbed too."""
    real_shape = "AIza" + "A" * 35
    leaked = f"401: invalid api key {real_shape} on request id req-42"
    scrubbed = _scrub_string(leaked)
    assert real_shape not in scrubbed
    assert "AIza<redacted>" in scrubbed
    # The non-secret context survives.
    assert "req-42" in scrubbed
