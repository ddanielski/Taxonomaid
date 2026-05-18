"""Rule engine: pure matching, scoring, and coherence checks.

The engine is stateless beyond its constructed rule set; it does no I/O.
File content is already extracted by the dispatcher and passed in.
"""

from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from taxonomaid.domain import CoherenceSpec, MatchSpec, Rule
from taxonomaid.services.year_extractor import detect_year

_TEMPLATE_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r"\{(\w+)\}")
_DESTINATION_YEAR: Final[re.Pattern[str]] = re.compile(r"(?<!\d)(?:19\d{2}|20\d{2})(?!\d)")


def _guess_mime_type(filename: str) -> str | None:
    """Return a best-effort MIME type for ``filename``, or ``None``.

    Uses the stdlib :mod:`mimetypes` module, which is purely
    extension-based - no content sniffing, no dependency on
    ``python-magic``. That's deliberate: sniff-based MIME detection
    would need a separate Phase-7-style integration with the text
    extractor, and the extension-only behaviour matches what users
    expect from a rule schema authored against filenames.
    """
    mime, _encoding = mimetypes.guess_type(filename)
    return mime


@dataclass(frozen=True, slots=True)
class RuleMatch:
    """Outcome of running the rule engine against a single file.

    Attributes:
        rule: The selected rule, if any.
        destination: The fully resolved destination path, if a rule won.
    """

    rule: Rule | None
    destination: Path | None

    @property
    def matched(self) -> bool:
        """``True`` iff a rule was selected and a destination resolved."""
        return self.rule is not None and self.destination is not None


_NO_MATCH: Final[RuleMatch] = RuleMatch(rule=None, destination=None)


class RuleEngine:
    """Pure rule matcher.

    The engine holds no state beyond its constructed rule set. It does no
    I/O; reading file content is the dispatcher's responsibility.
    """

    def __init__(self, rules: tuple[Rule, ...]) -> None:
        """Construct an engine over a frozen rule set.

        Args:
            rules: The full ordered set of rules (hand + auto-promoted).
                The engine reorders by score internally.
        """
        self._rules = tuple(sorted(rules, key=lambda r: r.score, reverse=True))

    @property
    def rules(self) -> tuple[Rule, ...]:
        """The rule set, ordered by descending score."""
        return self._rules

    def match(self, *, filename: str, ext: str, content: str | None) -> RuleMatch:
        """Run the rule set against a single file's metadata.

        The first rule whose predicate, template substitution, and
        coherence guards all succeed wins; rules are pre-sorted by
        ``weight * confidence``.

        Args:
            filename: Bare filename (no path components).
            ext: File extension including the leading dot.
            content: Optional extracted text excerpt.

        Returns:
            A populated :class:`RuleMatch`, or ``_NO_MATCH`` when nothing
            fired.
        """
        normalised_ext = ext.lower()
        for rule in self._rules:
            if not _matches_predicate(rule.match, filename, normalised_ext, content):
                continue
            destination = _resolve_template(rule.destination_template, filename, content)
            if destination is None:
                continue
            if not _passes_coherence(rule.coherence, filename, content, destination):
                continue
            return RuleMatch(rule=rule, destination=destination)
        return _NO_MATCH


def _matches_predicate(
    spec: MatchSpec,
    filename: str,
    ext: str,
    content: str | None,
) -> bool:
    if spec.ext is not None and ext not in spec.ext:
        return False
    if spec.filename_regex is not None and re.search(spec.filename_regex, filename) is None:
        return False
    if spec.mime_types is not None:
        guessed = _guess_mime_type(filename)
        if guessed is None or guessed not in spec.mime_types:
            return False
    if spec.content_keywords is not None:
        haystack = content or ""
        for needle in spec.content_keywords:
            if needle not in haystack:
                return False
    return True


def _resolve_template(
    template: str,
    filename: str,
    content: str | None,
) -> Path | None:
    placeholders = set(_TEMPLATE_PLACEHOLDER.findall(template))
    if not placeholders:
        return Path(template)

    substitutions: dict[str, str] = {}
    if "year" in placeholders:
        year = detect_year(filename, content)
        if year is None:
            return None
        substitutions["year"] = str(year)

    unresolved = placeholders - substitutions.keys()
    if unresolved:
        return None

    resolved = _TEMPLATE_PLACEHOLDER.sub(lambda m: substitutions[m.group(1)], template)
    return Path(resolved)


def _passes_coherence(
    coherence: CoherenceSpec,
    filename: str,
    content: str | None,
    destination: Path,
) -> bool:
    if coherence.year_match:
        file_year = detect_year(filename, content)
        if file_year is None:
            return False
        dest_match = _DESTINATION_YEAR.search(str(destination))
        if dest_match is None:
            return False
        if int(dest_match.group(0)) != file_year:
            return False
    return True
