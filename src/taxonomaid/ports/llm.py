"""LLM provider port and its response shape."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Structured output of a single classification call.

    Attributes:
        destination: Directory path the model wants the file placed in,
            *relative* to the watch's ``destination_root``.
        confidence: Self-reported confidence in ``[0, 1]``. The dispatcher
            uses this single signal to gate every action: high confidence
            allows folder creation, medium confidence requires the folder
            to already exist, low confidence parks the file in
            ``_unsorted/``.
        reason: Short justification, recorded in ``decisions.jsonl`` for
            audit and miner training.
    """

    destination: Path
    confidence: float
    reason: str


@runtime_checkable
class LLMProvider(Protocol):
    """Single-call, prompt-in / structured-out LLM interface.

    Implementations are expected to retry transient failures internally and
    raise :class:`taxonomaid.domain.LLMError` for terminal errors.
    """

    async def classify(
        self,
        *,
        filename: str,
        excerpt: str,
        candidate_destinations: tuple[Path, ...],
        prior_user_moves: tuple[tuple[str, Path], ...] = (),
    ) -> LLMResponse:
        """Classify a file into one of the candidate destinations.

        Args:
            filename: Bare filename (no path components).
            excerpt: First few KB of extracted text content.
            candidate_destinations: Closed menu of allowed destinations,
                relative to the watch's destination root.
            prior_user_moves: ``(filename, destination)`` pairs the user
                previously moved similar files to, injected as positive
                bias.

        Returns:
            A populated :class:`LLMResponse`.

        Raises:
            taxonomaid.domain.LLMError: On unrecoverable provider errors or
                an unparseable response.
        """
        ...
