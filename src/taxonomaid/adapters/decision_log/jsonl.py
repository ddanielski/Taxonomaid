"""JSONL-backed decision log adapter.

One JSON object per line, atomic appends via ``open(...,'a')`` with a
trailing newline so a partial write can never corrupt earlier entries.
``fcntl.flock`` serialises concurrent writers (the dispatcher daemon
plus a one-shot ``taxonomaid mine`` invocation, for instance) so a
JSON line over ``PIPE_BUF`` bytes - reachable once the ``reason`` field
is populated - can't interleave with another writer's bytes. The
append itself runs in an :func:`asyncio.to_thread` worker so a slow
disk - common on NAS bind-mounts - doesn't block the dispatcher's
event loop.

The decision log is **best-effort durable**: we don't ``fsync`` after
every line because the audit trail tolerates the loss of the most
recent decisions on a power cut. The pending-decision log uses a
stricter policy (see :mod:`taxonomaid.adapters.pending_log.jsonl`).

Trust boundary
--------------

The decision log records ``source`` verbatim and the miner promotes
entries whose source is in ``_PROMOTABLE_SOURCES`` (``LLM``,
``NOTIFIER_CONFIRMED``, ``USER_OVERRIDE``). Anyone with write access
to ``data/decisions.jsonl`` can therefore forge entries that
influence the rule corpus. We treat the file's filesystem
permissions as the trust boundary: standard ``chmod 600`` on the
file plus the systemd unit's ``ProtectSystem=strict`` /
``ProtectHome=read-only`` posture is the recommended hardening. If
you ever expose this file over a network share, mount it read-only
or split mining onto a separate runtime that holds its own copy.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from taxonomaid.domain import Decision, DecisionSource, FileSystemError

_log = structlog.get_logger("taxonomaid.decision_log")


class JsonlDecisionLog:
    """Append-only :class:`taxonomaid.ports.DecisionLog` backed by a JSONL file."""

    def __init__(self, path: Path) -> None:
        """Initialise the log, creating the parent directory if needed.

        Args:
            path: Destination file. Created on first append; the parent
                directory is created eagerly.
        """
        self._path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            msg = f"failed to prepare decision-log directory {path.parent}: {exc}"
            raise FileSystemError(msg) from exc

    @property
    def path(self) -> Path:
        """Backing file path."""
        return self._path

    async def append(self, decision: Decision) -> None:
        """Append ``decision`` as a single JSON line, off the event loop."""
        line = json.dumps(_serialise(decision), separators=(",", ":"), sort_keys=True)
        await asyncio.to_thread(self._sync_append, line + "\n")

    def _sync_append(self, line: str) -> None:
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                # ``flock`` keeps a separate ``taxonomaid mine`` /
                # ``taxonomaid review`` invocation from interleaving
                # bytes with the dispatcher's writes. POSIX guarantees
                # appends under O_APPEND are atomic only for writes
                # below ``PIPE_BUF`` (~4 KiB); JSON lines past the
                # ``reason`` field can exceed that.
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    fh.write(line)
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            msg = f"failed to append to decision log {self._path}: {exc}"
            raise FileSystemError(msg) from exc

    async def replay(self) -> AsyncIterator[Decision]:
        """Yield every decision in insertion order.

        Returns an empty iterator if the file does not yet exist.
        Malformed JSON lines (e.g. a partial flush after a crash, or a
        manual edit gone wrong) are logged and skipped rather than
        aborting replay - the miner depends on this stream and should
        survive one corrupt byte.
        """
        if not self._path.exists():
            return
        replayed = 0
        skipped = 0
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, start=1):
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        skipped += 1
                        _log.warning(
                            "decision_log_skip_bad_line",
                            path=str(self._path),
                            line=lineno,
                            error=str(exc),
                        )
                        continue
                    try:
                        decision = _deserialise(payload)
                    except (KeyError, ValueError, TypeError) as exc:
                        skipped += 1
                        _log.warning(
                            "decision_log_skip_bad_record",
                            path=str(self._path),
                            line=lineno,
                            error=str(exc),
                        )
                        continue
                    replayed += 1
                    yield decision
        except OSError as exc:
            msg = f"failed to read decision log {self._path}: {exc}"
            raise FileSystemError(msg) from exc
        if skipped:
            _log.info(
                "decision_log_replay_complete",
                path=str(self._path),
                replayed=replayed,
                skipped=skipped,
            )


def _serialise(decision: Decision) -> dict[str, Any]:
    return {
        "decision_id": decision.decision_id,
        "ts": decision.ts.isoformat(),
        "file": str(decision.file),
        "destination": str(decision.destination),
        "source": decision.source.value,
        "confidence": decision.confidence,
        "rule_id": decision.rule_id,
        "reason": decision.reason,
        # MappingProxyType is itself not JSON-serializable; copy back
        # to a plain dict for the wire format.
        "features": dict(decision.features),
    }


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _deserialise(payload: dict[str, Any]) -> Decision:
    raw_features = payload.get("features") or {}
    if not isinstance(raw_features, dict):
        msg = f"decision-log entry has non-mapping features: {raw_features!r}"
        raise FileSystemError(msg)

    return Decision(
        decision_id=str(payload["decision_id"]),
        ts=datetime.fromisoformat(str(payload["ts"])),
        file=Path(str(payload["file"])),
        destination=Path(str(payload["destination"])),
        source=DecisionSource(str(payload["source"])),
        confidence=float(payload["confidence"]),
        rule_id=_optional_str(payload.get("rule_id")),
        reason=_optional_str(payload.get("reason")),
        features=dict(raw_features),
    )
