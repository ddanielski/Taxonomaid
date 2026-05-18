"""Pure domain models with zero I/O and zero internal dependencies.

Every type in this subpackage is immutable (frozen ``dataclass``) and has no
import edges into the rest of the package. This is the foundation of the
hexagonal layering enforced by ``import-linter``.
"""

from __future__ import annotations

from taxonomaid.domain.decision import Decision, DecisionSource
from taxonomaid.domain.errors import (
    ConfigError,
    FileSystemError,
    LLMError,
    NotifierError,
    RuleError,
    TaxonomaidError,
)
from taxonomaid.domain.file_event import FileEvent, FileEventKind
from taxonomaid.domain.pending import PendingDecision, PendingState
from taxonomaid.domain.rule import CoherenceSpec, MatchSpec, Rule, RuleSource

__all__ = [
    "CoherenceSpec",
    "ConfigError",
    "Decision",
    "DecisionSource",
    "FileEvent",
    "FileEventKind",
    "FileSystemError",
    "LLMError",
    "MatchSpec",
    "NotifierError",
    "PendingDecision",
    "PendingState",
    "Rule",
    "RuleError",
    "RuleSource",
    "TaxonomaidError",
]
