"""Runtime ``isinstance`` checks for every Fake against its Protocol.

The ports in :mod:`taxonomaid.ports` are all declared
``@runtime_checkable``. ``mypy`` already validates structural conformance
at type-check time, but it can't catch a Fake that drifts after a port
gains a new method - the Fake silently no longer satisfies the protocol
and integration tests start using a degraded stub.

These tests close that gap: a single ``isinstance(fake, Port)``
assertion per Fake catches drift the day it happens.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from taxonomaid.adapters.clock import SystemClock
from taxonomaid.adapters.decision_log import JsonlDecisionLog
from taxonomaid.adapters.filesystem import LocalFilesystem
from taxonomaid.adapters.pending_log import JsonlPendingLog
from taxonomaid.ports import (
    Clock,
    DecisionLog,
    FilesystemPort,
    LLMProvider,
    NotifierInbound,
    NotifierOutbound,
    PendingLog,
    Watcher,
)
from tests.conftest import (
    FakeClock,
    FakeFilesystem,
    FakeNotifierInbound,
    FakeNotifierOutbound,
    FakeWatcher,
    RecordedLLM,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("fake", "port"),
    [
        (FakeClock(), Clock),
        (FakeFilesystem(), FilesystemPort),
        (FakeWatcher([]), Watcher),
        (RecordedLLM([]), LLMProvider),
        (FakeNotifierOutbound(), NotifierOutbound),
        (FakeNotifierInbound([]), NotifierInbound),
    ],
)
def test_fake_satisfies_port_runtime_isinstance(fake: object, port: type) -> None:
    """The Fake satisfies its port at runtime.

    If this fires, the port grew a method the Fake doesn't
    implement (or vice versa). Add the missing surface to the
    Fake and the port reference in this test stays a single
    parameter line.
    """
    assert isinstance(fake, port)


def test_real_adapters_satisfy_port_runtime_isinstance() -> None:
    """Spot-check the production adapters too.

    Two adapter types deliberately - LocalFilesystem (the
    canonical FilesystemPort impl) and SystemClock (the canonical
    Clock impl) - so a Protocol drift in those ports is caught.
    The remaining ports (LLMProvider, NotifierOutbound, ...) have
    multiple implementations and are sensitive to construction
    order; the Fake conformance above already covers the shape
    contract.
    """
    assert isinstance(LocalFilesystem(), FilesystemPort)
    assert isinstance(SystemClock(), Clock)


def test_jsonl_logs_satisfy_runtime_isinstance(tmp_path: Path) -> None:
    """JsonlDecisionLog and JsonlPendingLog satisfy their ports."""
    assert isinstance(JsonlDecisionLog(tmp_path / "decisions.jsonl"), DecisionLog)
    assert isinstance(JsonlPendingLog(tmp_path / "pending.jsonl"), PendingLog)
