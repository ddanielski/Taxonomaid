"""Unit tests for the system clock adapter."""

from __future__ import annotations

import pytest

from taxonomaid.adapters.clock import SystemClock

pytestmark = pytest.mark.unit


def test_system_clock_now_is_timezone_aware() -> None:
    assert SystemClock().now().tzinfo is not None
