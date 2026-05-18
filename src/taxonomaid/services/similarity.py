"""Filename-similarity index used to bias LLM prompts.

Phase 4 ships a token-based Jaccard similarity (no extra dependencies);
swapping in a sentence-transformers embedding lookup is a future change
behind the same interface.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from collections.abc import Iterable
from pathlib import Path
from typing import Final

_TOKEN_SPLIT: Final[re.Pattern[str]] = re.compile(r"[\W_]+")
_MIN_TOKEN_LEN: Final[int] = 3
# Hard cap on resident samples. The dispatcher's warm-up applies the
# same cap, but runtime additions also need eviction so a long-running
# daemon doesn't grow the index without bound.
_DEFAULT_MAX_SAMPLES: Final[int] = 50_000


def _tokens(name: str) -> frozenset[str]:
    stem = Path(name).stem.lower()
    return frozenset(part for part in _TOKEN_SPLIT.split(stem) if len(part) >= _MIN_TOKEN_LEN)


class _Sample:
    """One historical placement: filename, destination, token set, monotonic id.

    The ``id`` is a strictly-increasing counter assigned at insertion
    time. Postings in :class:`SimilarityIndex._by_token` reference
    ``id`` rather than list-index because eviction shifts indices,
    which would invalidate every posting otherwise.
    """

    __slots__ = ("destination", "filename", "id", "tokens")

    def __init__(self, *, sample_id: int, filename: str, destination: Path) -> None:
        self.id = sample_id
        self.filename = filename
        self.destination = destination
        self.tokens: frozenset[str] = _tokens(filename)


class SimilarityIndex:
    """Token-Jaccard index over historical placements.

    Build by calling :meth:`add` for each historical decision; query
    with :meth:`top_matches` to get the N most-similar prior
    placements for a new filename.

    The index is bounded: once :attr:`max_samples` is reached, the
    oldest sample is evicted FIFO and its postings are pruned. This
    keeps the resident set predictable on a long-running NAS daemon
    where the underlying ``decisions.jsonl`` grows for years.
    """

    def __init__(self, *, max_samples: int = _DEFAULT_MAX_SAMPLES) -> None:
        if max_samples <= 0:
            msg = "max_samples must be a positive integer"
            raise ValueError(msg)
        self._max_samples = max_samples
        self._samples: dict[int, _Sample] = {}
        self._order: deque[int] = deque()
        self._by_token: dict[str, set[int]] = defaultdict(set)
        self._next_id: int = 0

    @property
    def max_samples(self) -> int:
        """Hard cap on resident samples; the oldest is evicted on overflow."""
        return self._max_samples

    @property
    def samples(self) -> tuple[_Sample, ...]:
        """Snapshot of resident samples, oldest first (for tests)."""
        return tuple(self._samples[sid] for sid in self._order)

    def add(self, *, filename: str, destination: Path) -> None:
        """Index a single historical placement, evicting the oldest if full.

        Deduplicates on ``(filename, destination)``: a re-add of the
        same pair (e.g. a crash-recovery replay that revisits a
        decision) is a no-op rather than counting twice. This keeps
        the top-K scoring honest if the dispatcher ever ends up
        re-emitting a placement.
        """
        if self._is_duplicate(filename=filename, destination=destination):
            return
        sid = self._next_id
        self._next_id += 1
        sample = _Sample(sample_id=sid, filename=filename, destination=destination)
        self._samples[sid] = sample
        self._order.append(sid)
        for token in sample.tokens:
            self._by_token[token].add(sid)
        if len(self._order) > self._max_samples:
            self._evict_oldest()

    def _is_duplicate(self, *, filename: str, destination: Path) -> bool:
        """Cheap dedup check using token overlap as a prefilter."""
        tokens = _tokens(filename)
        if not tokens:
            return False
        candidate_ids: set[int] = set()
        for token in tokens:
            postings = self._by_token.get(token)
            if postings:
                candidate_ids.update(postings)
        for sid in candidate_ids:
            sample = self._samples.get(sid)
            if sample is None:
                continue
            if sample.filename == filename and sample.destination == destination:
                return True
        return False

    def add_many(self, samples: Iterable[tuple[str, Path]]) -> None:
        """Bulk-load samples; equivalent to calling :meth:`add` in a loop."""
        for filename, destination in samples:
            self.add(filename=filename, destination=destination)

    def top_matches(
        self,
        filename: str,
        *,
        limit: int = 5,
        min_score: float = 0.2,
    ) -> tuple[tuple[str, Path], ...]:
        """Return the top ``limit`` similar prior placements.

        Args:
            filename: The new file's bare name.
            limit: Cap on the number of results.
            min_score: Drop matches whose Jaccard score falls below
                this threshold.

        Returns:
            ``(filename, destination)`` pairs ordered by descending
            Jaccard similarity.
        """
        target = _tokens(filename)
        if not target:
            return ()
        candidate_ids: set[int] = set()
        for token in target:
            postings = self._by_token.get(token)
            if postings:
                candidate_ids.update(postings)
        scored: list[tuple[float, _Sample]] = []
        for sid in candidate_ids:
            sample = self._samples.get(sid)
            if sample is None:
                continue
            denom = len(target | sample.tokens)
            if denom == 0:
                continue
            score = len(target & sample.tokens) / denom
            if score < min_score:
                continue
            scored.append((score, sample))
        scored.sort(key=lambda item: item[0], reverse=True)
        return tuple((s.filename, s.destination) for _, s in scored[:limit])

    def _evict_oldest(self) -> None:
        sid = self._order.popleft()
        sample = self._samples.pop(sid, None)
        if sample is None:
            return
        for token in sample.tokens:
            postings = self._by_token.get(token)
            if postings is None:
                continue
            postings.discard(sid)
            if not postings:
                self._by_token.pop(token, None)
