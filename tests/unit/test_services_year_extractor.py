"""Unit tests for :mod:`taxonomaid.services.year_extractor`."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from taxonomaid.services.year_extractor import detect_year

pytestmark = pytest.mark.unit


def test_detect_year_in_filename() -> None:
    assert detect_year("tax_2025.pdf") == 2025


def test_detect_year_picks_max_when_multiple() -> None:
    assert detect_year("amended_2024_for_2023.pdf") == 2024


def test_detect_year_searches_content_when_filename_lacks_one() -> None:
    assert detect_year("tax_form.pdf", "Filed for 2022.") == 2022


def test_detect_year_returns_none_when_absent() -> None:
    assert detect_year("invoice.pdf", "no year here") is None


def test_detect_year_ignores_3_digit_numbers() -> None:
    assert detect_year("file_999.pdf") is None


def test_detect_year_skips_none_sources() -> None:
    assert detect_year(None, "report 2026") == 2026


@given(year=st.integers(min_value=1900, max_value=2099))
def test_detect_year_round_trips_any_valid_year(year: int) -> None:
    assert detect_year(f"file_{year}.pdf") == year
