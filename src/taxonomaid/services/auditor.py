"""Directory auditor.

Walks each destination, checks structural coherence (filename-only first;
content extraction is reserved for ambiguous cases handled in a later
phase), and emits :class:`AuditFinding` records the operator can review.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

from taxonomaid.services.year_extractor import detect_year

if TYPE_CHECKING:
    from taxonomaid.config import WatchConfig

_TOKEN_SPLIT: Final[re.Pattern[str]] = re.compile(r"[\W_]+")
_MIN_TOKEN_LEN: Final[int] = 3
_DEFAULT_MIN_FILES: Final[int] = 4
_DEFAULT_TOKEN_SUPPORT: Final[float] = 0.5
_DEFAULT_YEAR_AGREEMENT: Final[float] = 0.8
_DEFAULT_UNSORTED_MIN_FILES: Final[int] = 5
_DEFAULT_UNSORTED_MIN_AGE_DAYS: Final[int] = 7


class FindingKind(StrEnum):
    """Type of incoherence reported by the auditor."""

    YEAR_DRIFT = "year_drift"
    CATEGORY_DRIFT = "category_drift"
    UNSORTED_BACKLOG = "unsorted_backlog"


@dataclass(frozen=True, slots=True)
class AuditFinding:
    """A single coherence problem the auditor found.

    Attributes:
        kind: The kind of drift.
        directory: Absolute path to the offending directory.
        sample_count: Number of files inspected.
        details: Human-readable supporting detail (e.g. expected year vs
            observed years).
        offenders: A few example filenames that broke the pattern.
    """

    kind: FindingKind
    directory: Path
    sample_count: int
    details: str
    offenders: tuple[str, ...]


class Auditor:
    """Read-only directory coherence checker.

    Args:
        min_files: Skip directories with fewer than this many files.
        token_support: Minimum fraction of files that must share a token
            for the directory to be considered category-coherent.
        year_agreement: Fraction of files that must mention the
            destination year for the directory to clear the year guard.
    """

    def __init__(
        self,
        *,
        min_files: int = _DEFAULT_MIN_FILES,
        token_support: float = _DEFAULT_TOKEN_SUPPORT,
        year_agreement: float = _DEFAULT_YEAR_AGREEMENT,
        unsorted_min_files: int = _DEFAULT_UNSORTED_MIN_FILES,
        unsorted_min_age_days: int = _DEFAULT_UNSORTED_MIN_AGE_DAYS,
    ) -> None:
        self._min_files = min_files
        self._token_support = token_support
        self._year_agreement = year_agreement
        self._unsorted_min_files = unsorted_min_files
        self._unsorted_min_age_days = unsorted_min_age_days

    def audit(
        self,
        roots: Iterable[Path],
        *,
        watches: Iterable[WatchConfig] | None = None,
    ) -> tuple[AuditFinding, ...]:
        """Run the audit across every directory under each root.

        Args:
            roots: Destination roots to walk for year + category
                drift. Existing callers can ignore the new ``watches``
                argument and keep their behaviour.
            watches: Optional iterable of :class:`WatchConfig`. When
                supplied, each watch's ``_unsorted/`` (or whatever
                ``unsorted_dir`` is configured) is also checked for
                a backlog of long-pending files - the auditor's
                "you're forgetting to review parked files" signal.
        """
        findings: list[AuditFinding] = []
        for root in roots:
            if not root.is_dir():
                continue
            for directory in _iter_directories(root):
                findings.extend(self._audit_directory(directory))

        if watches is not None and self._unsorted_min_files > 0:
            for watch in watches:
                finding = self._check_unsorted_backlog(watch)
                if finding is not None:
                    findings.append(finding)

        return tuple(findings)

    def _audit_directory(self, directory: Path) -> list[AuditFinding]:
        try:
            files = [p for p in directory.iterdir() if p.is_file() and not p.name.startswith(".")]
        except OSError:
            # A subfolder may have its permissions tightened between the
            # walker yielding it and us trying to list it. On a shared
            # NAS that's plausible during the audit; skip the directory
            # rather than letting one EPERM abort the whole scan.
            return []
        if len(files) < self._min_files:
            return []

        findings: list[AuditFinding] = []
        if (year_finding := self._check_year_drift(directory, files)) is not None:
            findings.append(year_finding)
        if (token_finding := self._check_token_drift(directory, files)) is not None:
            findings.append(token_finding)
        return findings

    def _check_year_drift(
        self,
        directory: Path,
        files: list[Path],
    ) -> AuditFinding | None:
        # Use the same year-selection logic as the file side so a
        # nested layout like ``/archive/2023/Finance/2024/`` doesn't
        # trigger spurious drift findings: ``detect_year`` returns the
        # *latest* year found in the leaf name, which is the operator
        # intent ("the inner directory wins"), and matches what the
        # dispatcher's rule engine consults for the file's own year.
        expected = detect_year(directory.name)
        if expected is None:
            return None
        offenders: list[str] = []
        for path in files:
            year = detect_year(path.name)
            if year is None or year != expected:
                offenders.append(path.name)
        if len(offenders) / len(files) <= 1.0 - self._year_agreement:
            return None
        return AuditFinding(
            kind=FindingKind.YEAR_DRIFT,
            directory=directory,
            sample_count=len(files),
            details=(
                f"directory mentions {expected}, {len(offenders)}/{len(files)} files don't match"
            ),
            offenders=tuple(offenders[:5]),
        )

    def _check_unsorted_backlog(self, watch: WatchConfig) -> AuditFinding | None:
        """Report when ``_unsorted/`` has accumulated long-pending files.

        Files newer than ``unsorted_min_age_days`` are excluded so a
        recent burst (the operator hasn't had time to review yet)
        doesn't trigger the finding. Once at least
        ``unsorted_min_files`` files clear the age threshold, we
        emit a finding so the operator gets a Telegram nudge via
        ``audit --notify``.
        """
        unsorted_dir = (watch.destination_root / watch.unsorted_dir).resolve()
        if not unsorted_dir.is_dir():
            return None
        try:
            entries = list(unsorted_dir.iterdir())
        except OSError:
            return None

        cutoff = datetime.now(tz=UTC) - timedelta(days=self._unsorted_min_age_days)
        old_files: list[tuple[Path, datetime]] = []
        for entry in entries:
            try:
                if not entry.is_file() or entry.name.startswith("."):
                    continue
                mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=UTC)
            except OSError:
                continue
            if mtime <= cutoff:
                old_files.append((entry, mtime))

        if len(old_files) < self._unsorted_min_files:
            return None

        old_files.sort(key=lambda pair: pair[1])  # oldest first
        oldest_age = (datetime.now(tz=UTC) - old_files[0][1]).days
        sample_names = tuple(p.name for p, _ in old_files[:5])
        return AuditFinding(
            kind=FindingKind.UNSORTED_BACKLOG,
            directory=unsorted_dir,
            sample_count=len(old_files),
            details=(
                f"{len(old_files)} file(s) waiting at least "
                f"{self._unsorted_min_age_days} day(s); oldest is "
                f"{oldest_age} day(s) old"
            ),
            offenders=sample_names,
        )

    def _check_token_drift(
        self,
        directory: Path,
        files: list[Path],
    ) -> AuditFinding | None:
        token_counts: Counter[str] = Counter()
        for path in files:
            for token in _filename_tokens(path.name):
                token_counts[token] += 1
        if not token_counts:
            return AuditFinding(
                kind=FindingKind.CATEGORY_DRIFT,
                directory=directory,
                sample_count=len(files),
                details="no shared tokens across filenames",
                offenders=tuple(p.name for p in files[:5]),
            )
        most_common_token, support = token_counts.most_common(1)[0]
        if support / len(files) >= self._token_support:
            return None
        return AuditFinding(
            kind=FindingKind.CATEGORY_DRIFT,
            directory=directory,
            sample_count=len(files),
            details=(f"top token {most_common_token!r} only covers {support}/{len(files)} files"),
            offenders=tuple(p.name for p in files[:5]),
        )


def _iter_directories(root: Path) -> Iterable[Path]:
    """Yield every directory under ``root`` except hidden trees.

    Walks manually instead of via :meth:`Path.rglob` for two reasons:

    * Pruning: ``rglob`` can't skip a subtree once descent has started,
      so a large ``.git`` or ``node_modules`` would be fully visited.
      Manual walking lets us drop hidden directories before recursing.
    * Resilience: a single ``PermissionError`` mid-tree aborts ``rglob``
      with a stack trace; the manual walk catches per-directory ``OSError``
      and continues, which matters on NAS bind-mounts where one folder
      may transiently be unreadable.
    """
    queue: list[Path] = [root]
    while queue:
        directory = queue.pop()
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            if entry.name.startswith("."):
                continue
            yield entry
            queue.append(entry)


def _filename_tokens(name: str) -> set[str]:
    stem = Path(name).stem.lower()
    return {part for part in _TOKEN_SPLIT.split(stem) if len(part) >= _MIN_TOKEN_LEN}
