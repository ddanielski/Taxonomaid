"""Unit tests for the error hierarchy."""

from __future__ import annotations

import pytest

from taxonomaid.domain import (
    ConfigError,
    FileSystemError,
    LLMError,
    NotifierError,
    RuleError,
    TaxonomaidError,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "subclass",
    [ConfigError, RuleError, LLMError, NotifierError, FileSystemError],
)
def test_subclasses_inherit_from_root(subclass: type[Exception]) -> None:
    assert issubclass(subclass, TaxonomaidError)


def test_subclasses_can_be_caught_as_root() -> None:
    with pytest.raises(TaxonomaidError):
        raise ConfigError("boom")
