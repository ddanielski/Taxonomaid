"""Placeholder for opt-in external contract tests.

Real contract tests against Gemini and Telegram land in Phase 1.
Marked ``external`` so they are excluded from the default selection.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.external]


@pytest.mark.skip(reason="contract tests land in Phase 1")
def test_placeholder() -> None:
    pass
