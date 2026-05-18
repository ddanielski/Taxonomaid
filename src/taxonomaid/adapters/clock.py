"""Clock adapters."""

from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    """Clock backed by :func:`datetime.datetime.now` in UTC."""

    def now(self) -> datetime:
        """Return the current UTC time."""
        return datetime.now(UTC)
