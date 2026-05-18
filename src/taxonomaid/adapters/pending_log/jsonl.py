"""JSONL-backed pending-decision log.

Append-only on every state transition: the latest record for a given
``decision_id`` wins on replay. This avoids in-place rewrites that would
risk corrupting earlier entries on a crash.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from taxonomaid.domain import FileSystemError, PendingDecision, PendingState

_log = structlog.get_logger("taxonomaid.pending_log")

_VALID_SUCCESSORS: dict[PendingState, frozenset[PendingState]] = {
    PendingState.REQUESTED: frozenset({PendingState.ANSWERED, PendingState.APPLIED}),
    PendingState.ANSWERED: frozenset({PendingState.APPLIED}),
    PendingState.APPLIED: frozenset(),
}


class JsonlPendingLog:
    """Concrete :class:`taxonomaid.ports.PendingLog` over a JSONL file.

    A :class:`dict` of ``decision_id -> PendingDecision`` is kept in
    memory as the source of truth for reads. The file remains the
    durable source of truth - the cache is rebuilt from it on
    construction and updated synchronously on every append/transition.
    This keeps :meth:`get` and :meth:`replay` O(1) per entry instead of
    rescanning the file on every notifier reply.
    """

    def __init__(self, path: Path) -> None:
        """Initialise the log; create the parent directory eagerly and warm the cache."""
        self._path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            msg = f"failed to prepare pending-log directory {path.parent}: {exc}"
            raise FileSystemError(msg) from exc
        self._cache: dict[str, PendingDecision] = self._load_cache()

    @property
    def path(self) -> Path:
        """Backing file path."""
        return self._path

    async def append(self, pending: PendingDecision) -> None:
        """Append a new pending decision in state ``REQUESTED``."""
        await self._write(_serialise(pending))
        self._cache[pending.decision_id] = pending

    async def transition(self, decision_id: str, *, to: PendingState) -> None:
        """Append a state transition for ``decision_id``.

        Raises :class:`FileSystemError` if the decision is unknown or
        the requested transition isn't a valid successor of the current
        state. The legal predecessors are:

        * ``REQUESTED -> ANSWERED``
        * ``REQUESTED -> APPLIED`` (skipping ANSWERED is fine)
        * ``ANSWERED -> APPLIED``
        """
        existing = self._cache.get(decision_id)
        if existing is None:
            msg = f"unknown decision_id: {decision_id}"
            raise FileSystemError(msg)
        if to not in _VALID_SUCCESSORS[existing.state]:
            msg = (
                f"refusing transition for {decision_id}: "
                f"{existing.state.value} -> {to.value} is not a legal successor"
            )
            raise FileSystemError(msg)
        updated = _with_state(existing, to)
        await self._write(_serialise(updated))
        self._cache[decision_id] = updated

    async def get(self, decision_id: str) -> PendingDecision | None:
        """Return the latest version of ``decision_id``."""
        return self._cache.get(decision_id)

    async def replay(self) -> AsyncIterator[PendingDecision]:
        """Yield the latest version of every pending decision."""
        for entry in self._cache.values():
            yield entry

    async def replay_all(self) -> AsyncIterator[PendingDecision]:
        """Yield every record (including transitions) in insertion order.

        Always reads from disk; the in-memory cache only stores the
        latest record per decision_id. Malformed records (truncated
        JSON, missing fields after a schema change) are logged and
        skipped so an interrupted process recovery doesn't fail with
        a hard error - the same posture as :meth:`_load_cache` and
        :meth:`taxonomaid.adapters.decision_log.JsonlDecisionLog.replay`.
        """
        if not self._path.exists():
            return
        skipped = 0
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, start=1):
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        yield _deserialise(json.loads(raw))
                    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
                        skipped += 1
                        _log.warning(
                            "pending_log_skip_bad_line",
                            path=str(self._path),
                            line=lineno,
                            error=str(exc),
                        )
        except OSError as exc:
            msg = f"failed to read pending log {self._path}: {exc}"
            raise FileSystemError(msg) from exc
        if skipped:
            _log.info("pending_log_replay_summary", path=str(self._path), skipped=skipped)

    async def _write(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"
        await asyncio.to_thread(self._sync_write, line)

    def _sync_write(self, line: str) -> None:
        """Synchronous append with single-process locking + fsync.

        ``fcntl.flock`` keeps a separate ``taxonomaid mine`` /
        ``taxonomaid review`` invocation from interleaving bytes with
        the dispatcher's writes. The ``flush`` + ``fsync`` pair makes
        each pending state transition durable: if the dispatcher
        crashes between the ``move`` and the next transition, we still
        recover the in-progress decision on restart.
        """
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    fh.write(line)
                    fh.flush()
                    with contextlib.suppress(OSError):
                        os.fsync(fh.fileno())
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            msg = f"failed to append to pending log {self._path}: {exc}"
            raise FileSystemError(msg) from exc

    def _load_cache(self) -> dict[str, PendingDecision]:
        cache: dict[str, PendingDecision] = {}
        skipped = 0
        if not self._path.exists():
            return cache
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, start=1):
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        parsed = _deserialise(json.loads(raw))
                    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
                        skipped += 1
                        _log.warning(
                            "pending_log_skip_bad_line",
                            path=str(self._path),
                            line=lineno,
                            error=str(exc),
                        )
                        continue
                    cache[parsed.decision_id] = parsed
        except OSError as exc:
            msg = f"failed to warm pending-log cache from {self._path}: {exc}"
            raise FileSystemError(msg) from exc
        if skipped:
            _log.info(
                "pending_log_cache_warmed",
                path=str(self._path),
                kept=len(cache),
                skipped=skipped,
            )
        return cache


def _with_state(pending: PendingDecision, state: PendingState) -> PendingDecision:
    return PendingDecision(
        decision_id=pending.decision_id,
        ts=pending.ts,
        unsorted_path=pending.unsorted_path,
        proposed_destination=pending.proposed_destination,
        destination_root=pending.destination_root,
        confidence=pending.confidence,
        reason=pending.reason,
        state=state,
    )


def _serialise(pending: PendingDecision) -> dict[str, Any]:
    return {
        "decision_id": pending.decision_id,
        "ts": pending.ts.isoformat(),
        "unsorted_path": str(pending.unsorted_path),
        "proposed_destination": str(pending.proposed_destination),
        "destination_root": str(pending.destination_root),
        "confidence": pending.confidence,
        "reason": pending.reason,
        "state": pending.state.value,
    }


def _deserialise(payload: dict[str, Any]) -> PendingDecision:
    unsorted_path = Path(str(payload["unsorted_path"]))
    raw_root = payload.get("destination_root")
    # Pre-§1.4 records didn't store destination_root; reconstruct
    # structurally from unsorted_path. The invariant
    # ``unsorted_path == <destination_root>/<unsorted_dir>/<filename>``
    # makes two ``.parent`` calls peel back to the destination root.
    #
    # WARNING: this reconstruction depends on the config-time
    # invariant that ``unsorted_dir`` is a single segment (enforced
    # in :class:`taxonomaid.config.WatchConfig._single_segment_unsorted`).
    # If that invariant is ever relaxed, this branch will produce
    # the wrong root for legacy entries. A migration script that
    # rewrites legacy entries with an explicit ``destination_root``
    # field would be safer than relaxing the invariant in place.
    if raw_root is None:
        _log.warning(
            "pending_log_legacy_record_structural_recovery",
            unsorted_path=str(unsorted_path),
            reconstructed_root=str(unsorted_path.parent.parent),
            note=(
                "pre-§1.4 record; destination_root reconstructed from "
                "unsorted_path. Accurate only when unsorted_dir is a "
                "single segment (the current config-level guarantee)."
            ),
        )
        destination_root = unsorted_path.parent.parent
    else:
        destination_root = Path(str(raw_root))
    return PendingDecision(
        decision_id=str(payload["decision_id"]),
        ts=datetime.fromisoformat(str(payload["ts"])),
        unsorted_path=unsorted_path,
        proposed_destination=Path(str(payload["proposed_destination"])),
        destination_root=destination_root,
        confidence=float(payload["confidence"]),
        reason=str(payload["reason"]),
        state=PendingState(str(payload["state"])),
    )
