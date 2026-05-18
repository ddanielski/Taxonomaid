"""Unit tests for :mod:`taxonomaid.ports.notifier` value objects."""

from __future__ import annotations

import pytest

from taxonomaid.ports import NotifierResponse, NotifierResponseKind

pytestmark = pytest.mark.unit


def test_notifier_response_extra_outer_mapping_is_immutable() -> None:
    response = NotifierResponse(
        decision_id="d",
        kind=NotifierResponseKind.APPROVE,
        extra={"channel": "telegram"},
    )
    with pytest.raises(TypeError):
        response.extra["leak"] = "bad"  # type: ignore[index]
