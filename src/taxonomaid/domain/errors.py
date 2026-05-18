"""Exception hierarchy rooted at :class:`TaxonomaidError`.

Adapters wrap third-party exceptions in one of these subclasses before
re-raising so services see a stable error surface.
"""

from __future__ import annotations


class TaxonomaidError(Exception):
    """Base class for every error raised by Taxonomaid."""


class ConfigError(TaxonomaidError):
    """Configuration is missing, malformed, or fails validation."""


class RuleError(TaxonomaidError):
    """A rule definition or rule operation is invalid."""


class LLMError(TaxonomaidError):
    """The LLM provider returned an error or an unparseable response."""


class NotifierError(TaxonomaidError):
    """A notifier backend failed to send or receive a message."""


class FileSystemError(TaxonomaidError):
    """A filesystem operation failed in a way the daemon cannot recover from."""
