"""Year extraction helper.

Used by the rule engine for template substitution and coherence guards.
The pattern accepts any 4-digit year in 1900-2099; if multiple candidates
appear, the most recent one wins (a 2024-amended 2023 tax form should
file under the year of latest activity).
"""

from __future__ import annotations

import re
from typing import Final

_YEAR_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?<!\d)(?:19\d{2}|20\d{2})(?!\d)")


def detect_year(*sources: str | None) -> int | None:
    """Return the most recent 4-digit year detected across ``sources``.

    Args:
        *sources: Strings to scan in order (e.g., filename, then content).

    Returns:
        The largest year found in ``[1900, 2099]``, or ``None`` if none of
        the sources contained one.
    """
    found: list[int] = []
    for source in sources:
        if not source:
            continue
        found.extend(int(m.group(0)) for m in _YEAR_PATTERN.finditer(source))
    if not found:
        return None
    return max(found)
