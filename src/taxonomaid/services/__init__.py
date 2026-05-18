"""Public services API.

Modules in this package depend exclusively on :mod:`taxonomaid.ports`
and :mod:`taxonomaid.domain`. Concrete adapters are wired in by
:func:`taxonomaid.bootstrap.build_app`.

``Dispatcher`` is **deliberately not re-exported** here: importing it
transitively pulls in ``text_excerpt`` and its PDF / DOCX / RTF
extractor dependencies, which the ``mine`` / ``review`` / ``audit`` CLI
subcommands have no use for. Callers that need the dispatcher import
it directly from ``taxonomaid.services.dispatcher``.
"""

from __future__ import annotations

from taxonomaid.services.auditor import AuditFinding, Auditor, FindingKind
from taxonomaid.services.feedback import RecentlyMoved
from taxonomaid.services.health import (
    ProbeResult,
    ProbeStatus,
    probe_llm,
    probe_telegram,
    probe_telegram_chat_is_private,
)
from taxonomaid.services.miner import Miner, RuleProposal
from taxonomaid.services.review_session import ReviewPaths, ReviewQueue, ReviewSession
from taxonomaid.services.rule_engine import RuleEngine
from taxonomaid.services.similarity import SimilarityIndex

__all__ = [
    "AuditFinding",
    "Auditor",
    "FindingKind",
    "Miner",
    "ProbeResult",
    "ProbeStatus",
    "RecentlyMoved",
    "ReviewPaths",
    "ReviewQueue",
    "ReviewSession",
    "RuleEngine",
    "RuleProposal",
    "SimilarityIndex",
    "probe_llm",
    "probe_telegram",
    "probe_telegram_chat_is_private",
]
