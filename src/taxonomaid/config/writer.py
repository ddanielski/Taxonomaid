"""YAML writers for rules files.

The reverse of :func:`taxonomaid.config.loader.load_rules_file`. Rules
are serialised through their :class:`RuleConfig` pydantic counterparts so
the on-disk shape is exactly what the loader expects.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from taxonomaid.config.models import (
    CoherenceSpecConfig,
    MatchSpecConfig,
    RuleConfig,
    RulesConfig,
)
from taxonomaid.domain import FileSystemError, Rule


def rule_to_config(rule: Rule) -> RuleConfig:
    """Convert an in-memory :class:`Rule` back to its YAML counterpart."""
    return RuleConfig(
        id=rule.id,
        match=MatchSpecConfig(
            filename_regex=rule.match.filename_regex,
            ext=rule.match.ext,
            mime_types=rule.match.mime_types,
            content_keywords=rule.match.content_keywords,
        ),
        destination_template=rule.destination_template,
        coherence=CoherenceSpecConfig(year_match=rule.coherence.year_match),
        weight=rule.weight,
        confidence=rule.confidence,
        anchored=rule.anchored,
        source=rule.source,
        sample_count=rule.sample_count,
    )


def write_rules_file(path: Path, rules: tuple[Rule, ...]) -> None:
    """Atomically write a rules YAML file.

    Args:
        path: Destination file. Parent directories are created if needed.
        rules: Rules to serialise. May be empty, which writes ``rules: []``.

    Raises:
        FileSystemError: On any IO failure.
    """
    config = RulesConfig(rules=tuple(rule_to_config(r) for r in rules))
    payload = _to_yaml_dict(config)
    serialised = yaml.safe_dump(payload, sort_keys=False)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(serialised, encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        msg = f"failed to write {path}: {exc}"
        raise FileSystemError(msg) from exc


def _to_yaml_dict(config: RulesConfig) -> dict[str, Any]:
    rules: list[dict[str, Any]] = []
    for rule in config.rules:
        match: dict[str, Any] = {}
        if rule.match.filename_regex is not None:
            match["filename_regex"] = rule.match.filename_regex
        if rule.match.ext is not None:
            match["ext"] = list(rule.match.ext)
        if rule.match.mime_types is not None:
            match["mime_types"] = list(rule.match.mime_types)
        if rule.match.content_keywords is not None:
            match["content_keywords"] = list(rule.match.content_keywords)

        entry: dict[str, Any] = {
            "id": rule.id,
            "match": match,
            "destination_template": rule.destination_template,
        }
        if rule.coherence.year_match:
            entry["coherence"] = {"year_match": True}
        entry.update(
            {
                "weight": rule.weight,
                "confidence": rule.confidence,
                "anchored": rule.anchored,
                "source": rule.source.value,
                "sample_count": rule.sample_count,
            }
        )
        rules.append(entry)
    return {"rules": rules}
