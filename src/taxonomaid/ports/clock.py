"""Clock port.

Injecting time lets unit tests run deterministically without sleeping or
freezing the system clock.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Source of timezone-aware UTC timestamps."""

    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC ``datetime``."""
        ...
