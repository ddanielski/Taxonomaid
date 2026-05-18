"""Single source of truth for tunable defaults.

Every value here can be overridden via environment variables (typically
loaded from ``.env``) or via the matching field in a YAML config. The
priority chain is:

    YAML > env var > this default

Add new tunables here only, never duplicate them into the YAML examples
or the dataclass body.
"""

from __future__ import annotations

from typing import Final

DEFAULT_LLM_BASE_URL: Final[str] = "https://generativelanguage.googleapis.com/v1beta/openai/"
"""OpenAI-compatible endpoint for the default Gemini provider."""

DEFAULT_LLM_MODEL: Final[str] = "gemini-3.1-flash-lite"
"""Model name; cheap, low-latency Gemini tier with a 1M-token context."""

DEFAULT_LLM_REQUEST_TIMEOUT_S: Final[float] = 30.0
"""Per-request timeout for LLM HTTP calls."""

DEFAULT_LLM_MAX_EXCERPT_CHARS: Final[int] = 262144
"""Hard cap on the **character** length of the text excerpt fed to the LLM.

The unit is characters, not bytes - the dispatcher decodes each file
to text first and slices the result with ``str[:max_chars]``.
``262144`` (256 KiB) is roughly 64 K tokens for English prose -
about 6% of Gemini Flash Lite's 1M-token context, and well within
the free tier's per-minute budget at NAS workload rates (a few
files per hour). The previous 64 KiB default captured the first
page or two of most documents; 256 KiB captures the body of a
typical multi-page contract, manual, or long-form report end to
end, which materially improves classification on the long-tail
documents most likely to need it.

Override with the ``TAXONOMAID_LLM_MAX_EXCERPT_CHARS`` environment
variable or the ``max_excerpt_chars:`` key in ``llm.yaml``.

The legacy ``DEFAULT_LLM_MAX_EXCERPT_BYTES`` name is preserved as a
deprecated alias for one release; the corresponding env var
``TAXONOMAID_LLM_MAX_EXCERPT_BYTES`` is also still honoured at the
config-loading layer.
"""

DEFAULT_LLM_MAX_EXCERPT_BYTES: Final[int] = DEFAULT_LLM_MAX_EXCERPT_CHARS
"""Deprecated: use :data:`DEFAULT_LLM_MAX_EXCERPT_CHARS`. Same value."""

DEFAULT_AUTO_MOVE_THRESHOLD: Final[float] = 0.75
DEFAULT_AUTO_CREATE_FOLDER_THRESHOLD: Final[float] = 0.85
DEFAULT_AUTO_PROMOTE_RULE_THRESHOLD: Final[float] = 0.97

DEFAULT_NOTIFIER_POLL_TIMEOUT_S: Final[float] = 30.0
"""Long-poll timeout for the Telegram ``getUpdates`` listener."""
