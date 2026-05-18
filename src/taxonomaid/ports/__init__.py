"""Protocol-based interfaces for every external dependency.

Services depend exclusively on these protocols; concrete adapters live in
:mod:`taxonomaid.adapters` and are wired together by
:func:`taxonomaid.bootstrap.build_app`.
"""

from __future__ import annotations

from taxonomaid.ports.clock import Clock
from taxonomaid.ports.decision_log import DecisionLog
from taxonomaid.ports.filesystem import FilesystemPort
from taxonomaid.ports.llm import LLMProvider, LLMResponse
from taxonomaid.ports.notifier import (
    NotifierInbound,
    NotifierOutbound,
    NotifierResponse,
    NotifierResponseKind,
)
from taxonomaid.ports.pending_log import PendingLog
from taxonomaid.ports.watcher import Watcher

__all__ = [
    "Clock",
    "DecisionLog",
    "FilesystemPort",
    "LLMProvider",
    "LLMResponse",
    "NotifierInbound",
    "NotifierOutbound",
    "NotifierResponse",
    "NotifierResponseKind",
    "PendingLog",
    "Watcher",
]
