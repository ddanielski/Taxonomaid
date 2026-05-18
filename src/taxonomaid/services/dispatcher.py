"""Dispatcher: watcher → rule engine → LLM → notifier orchestrator.

Two concurrent tasks run inside :meth:`Dispatcher.run`:

- ``_watch_loop`` consumes filesystem events, runs the rule engine, falls
  through to the LLM if no rule matched, and either moves the file or
  parks it in ``_unsorted/`` while sending a notifier prompt.
- ``_inbound_loop`` consumes user replies from the notifier and applies
  them to the matching pending decision (approve / reject / propose).

Both tasks communicate through the :class:`PendingLog` and the
:class:`DecisionLog`; they never share in-memory state directly.

Crash-window recovery
---------------------

The dispatcher writes to two append-only logs and performs filesystem
moves in between. The order is intentional and the recovery semantics
are best-effort:

- ``_park_and_notify`` precomputes the eventual ``unsorted_path``
  (mkdir + collision-free name resolution), writes the
  ``pending_log`` entry (``flock`` + ``fsync``, durable) **before**
  the move, then performs the move, then writes the ``decision_log``
  "parked:" entry (best-effort, no fsync). The pending entry
  therefore exists durably even if a crash happens before the move
  completes. Failure modes:

  - **Crash after pending append, before move**: pending entry
    in ``REQUESTED`` with ``unsorted_path`` that doesn't exist.
    Detected at the next startup by
    :meth:`Dispatcher._recover_orphan_pending`, which transitions
    the orphan to ``APPLIED``. The source file is still at
    ``event.path``; the watcher will re-emit it as a fresh event.
  - **Crash after move, before decision append**: file at
    ``unsorted_path``, pending entry intact, audit log missing one
    "parked:" line. Inbound flow works normally; the audit gap is
    permanent but non-corrupting.
  - **Crash after decision append, before notifier**: same as
    above plus the user never sees a Telegram prompt. The parked
    file is recoverable from the pending log via
    ``taxonomaid review`` (when implemented) or by manually
    inspecting ``data/pending_decisions.jsonl``.

- ``_apply_response`` does ``move`` → ``decision_log.append`` →
  ``pending_log.transition``. A crash between the move and the
  transition leaves the file at its final destination with the
  pending entry still in ``REQUESTED`` or ``ANSWERED``. On restart,
  the Telegram inbound replays the matching update; the dispatcher
  looks up the pending decision, finds it is **not** ``APPLIED``,
  and re-enters ``_apply_response``. Before calling ``move`` the
  recovery short-circuit checks whether ``pending.unsorted_path``
  still exists - if it doesn't (the previous run moved it away),
  the dispatcher transitions the pending entry to ``APPLIED``
  without attempting another move, then continues. The audit-log
  entry from the first run remains the canonical record; the
  recovery path doesn't double-write it.

The pragmatic guarantee is therefore: **no file is ever lost or
silently overwritten**, and the pending log always converges back
to ``APPLIED`` after a restart. Combining ``move`` and
``transition`` into a single durable record is still a future
improvement that would let us drop the recovery short-circuit.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from taxonomaid.config import AppConfig
from taxonomaid.domain import (
    Decision,
    DecisionSource,
    FileEvent,
    FileEventKind,
    LLMError,
    NotifierError,
    PendingDecision,
    PendingState,
    Rule,
    TaxonomaidError,
)
from taxonomaid.ports import (
    Clock,
    DecisionLog,
    FilesystemPort,
    LLMProvider,
    LLMResponse,
    NotifierInbound,
    NotifierOutbound,
    NotifierResponse,
    NotifierResponseKind,
    PendingLog,
    Watcher,
)
from taxonomaid.services.circuit_breaker import CircuitState, LLMCircuit
from taxonomaid.services.feedback import RecentlyMoved
from taxonomaid.services.path_safety import (
    collision_free_path,
    safe_resolve,
    safe_unsorted_dir,
)
from taxonomaid.services.review_session import ReviewSession
from taxonomaid.services.rule_engine import RuleEngine
from taxonomaid.services.similarity import SimilarityIndex
from taxonomaid.services.text_excerpt import read_excerpt

if TYPE_CHECKING:
    from collections.abc import Iterable

_log = structlog.get_logger("taxonomaid.dispatcher")

_SIMILARITY_INDEXED_SOURCES: frozenset[DecisionSource] = frozenset(
    {DecisionSource.RULE, DecisionSource.LLM, DecisionSource.NOTIFIER_CONFIRMED}
)
# Cap the warm-up replay so a multi-year ``decisions.jsonl`` doesn't
# bloat the index every restart. The full log remains the authoritative
# audit trail; the similarity index is just a query accelerator for
# the LLM prompt and the most recent samples carry by far the highest
# signal. We keep the *tail* (newest decisions) on the assumption that
# user taste drifts over time.
#
# Until a real log-rotation policy lands (Phase 7), the operator can
# unblock a runaway log by manually archiving (``mv decisions.jsonl
# decisions.YYYY-MM.jsonl`` while the daemon is stopped); on next
# start the warm-up sees an empty current log and the similarity
# index repopulates as decisions arrive. The miner's replay also
# only reads the current file - mined-rule proposals will reflect
# only the post-rotation window, which is usually what the operator
# wants when archiving.
_SIMILARITY_INDEX_MAX_SAMPLES: int = 50_000

# Skip excerpt extraction for files larger than this; classify on filename
# alone instead. Prevents pathological PDFs from blocking a thread-pool
# worker or eating all the daemon's RAM.
_MAX_INPUT_BYTES_FOR_EXTRACTION: int = 50 * 1024 * 1024
# Bound the synchronous extraction call so a hung extractor can't
# stall the dispatcher forever.
_EXTRACTION_TIMEOUT_S: float = 30.0
# Cap the ``Decision.reason`` and ``PendingDecision.reason`` fields
# before persisting them to JSONL. The Telegram outbound truncates at
# 3000 chars to fit the 4096-char message limit; we use a slightly
# larger ceiling here so the audit trail still keeps a useful prefix
# of any verbose LLM justification, but no single decision line can
# blow up the average length of the log (which the miner replays line
# by line every run).
_MAX_LOG_REASON_CHARS: int = 4000


def _truncate_reason(text: str) -> str:
    """Bound a free-text ``reason`` so JSONL line lengths stay sane.

    Returns ``text`` unchanged when it already fits in
    :data:`_MAX_LOG_REASON_CHARS`; otherwise returns a prefix of
    ``_MAX_LOG_REASON_CHARS`` characters total (including the
    ellipsis) so the cap is observed exactly.
    """
    if len(text) <= _MAX_LOG_REASON_CHARS:
        return text
    return text[: _MAX_LOG_REASON_CHARS - 1] + "…"


@dataclass(frozen=True, slots=True)
class DispatcherDeps:
    """Constructor-injected dependencies of the dispatcher.

    Grouped into a single value object so tests can build it incrementally
    without long positional argument lists.
    """

    config: AppConfig
    rule_engine: RuleEngine
    llm: LLMProvider
    notifier_outbound: NotifierOutbound | None
    notifier_inbound: NotifierInbound | None
    filesystem: FilesystemPort
    decision_log: DecisionLog
    pending_log: PendingLog
    watcher: Watcher
    clock: Clock
    rule_engines_by_root: Mapping[Path, RuleEngine] | None = None
    """Optional per-watch rule engines, keyed by ``WatchConfig.path``.

    When set, the dispatcher routes events to the matching engine; the
    pooled :attr:`rule_engine` becomes the fallback for events whose
    watch root isn't in the map. This lets multi-watch deployments scope
    rules to a single root without polluting siblings.
    """
    similarity: SimilarityIndex | None = None
    debounce_s: float = 0.25
    similarity_top_k: int = 5
    review_session: ReviewSession | None = None
    """Optional review service. When set together with a Telegram
    outbound, the daemon answers ``/review`` commands and rule
    approve/reject button taps; otherwise those inbound events are
    logged as ``review_*`` warnings and ignored."""
    llm_circuit: LLMCircuit | None = None
    """Optional LLM availability circuit. When unset, every LLM
    error parks one file with a per-file Telegram prompt - fine
    for one-off failures, terrible for sustained outages. When set,
    consecutive failures trip the circuit; subsequent files are
    parked silently and one alert is sent. Bootstrap installs a
    default circuit; tests can pass ``None`` to keep the
    dispatcher's behaviour identical to pre-circuit semantics."""


class Dispatcher:
    """Top-level orchestration service."""

    def __init__(self, deps: DispatcherDeps) -> None:
        """Wire the dispatcher to its dependencies.

        Args:
            deps: Bundle of ports and configuration produced by
                :func:`taxonomaid.bootstrap.build_app`.
        """
        self._deps = deps
        self._recent = RecentlyMoved()

    @property
    def deps(self) -> DispatcherDeps:
        """Read-only view of the wired dependencies (for tests)."""
        return self._deps

    async def run(self) -> None:
        """Run the dispatcher until cancelled."""
        await self._recover_orphan_pending()
        await self._warm_similarity_index()
        await self._bootstrap_existing_files()
        watches = self._deps.config.watches.watches
        _log.info(
            "daemon_started",
            watches=len(watches),
            watch_paths=[str(w.path) for w in watches],
            destination_roots=[str(w.destination_root) for w in watches],
            llm_model=self._deps.config.llm.model,
            llm_base_url=str(self._deps.config.llm.base_url),
        )
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._watch_loop(), name="taxonomaid.watch")
                if self._deps.notifier_inbound is not None:
                    tg.create_task(self._inbound_loop(), name="taxonomaid.inbound")
        finally:
            _log.info("daemon_stopped")

    async def stop(self) -> None:
        """Signal an orderly shutdown.

        Sets the stop events on the watcher and inbound notifier and
        returns immediately - it does **not** wait for :meth:`run` to
        finish. The CLI's ``_run_until_signalled`` is what actually
        awaits the running ``run()`` task to drain; ``stop()`` is just
        the request to wind down. Renaming would be cleaner but
        breaking; the docstring carries the contract.
        """
        await self._deps.watcher.stop()
        if self._deps.notifier_inbound is not None:
            await self._deps.notifier_inbound.stop()

    async def bootstrap_all(self) -> None:
        """One-shot bootstrap walk for the ``taxonomaid bootstrap`` CLI.

        Like ``run()`` this runs orphan-pending recovery and warms
        the similarity index, but instead of starting the watcher
        loop it processes every existing file in every watch and
        returns. Used by the CLI command to onboard pre-existing
        files on demand (vs. the per-watch ``bootstrap_existing``
        flag that fires on daemon startup).
        """
        await self._recover_orphan_pending()
        await self._warm_similarity_index()
        await self._bootstrap_existing_files(force_all=True)

    async def _bootstrap_existing_files(self, *, force_all: bool = False) -> None:
        """Process pre-existing files in watches with ``bootstrap_existing=true``.

        ``inotify`` only fires on new events, so files that already
        live in a watch root when the daemon starts are otherwise
        invisible. With ``bootstrap_existing: true`` per watch, we
        walk the root once at startup and feed every regular file
        through the same pipeline as a fresh ADDED event.

        Files inside the watch's ``unsorted_dir`` are skipped - the
        crash-recovery path (``_recover_orphan_pending``) handles
        those, and re-classifying parked files would just churn the
        operator's review queue. Hidden files (``.foo``) are also
        skipped by convention.

        The walk is sequential: the LLM-roundtrip cadence enforced
        by ``_watch_loop`` is the rate limit and the same applies
        here. A bootstrap of 100 files at 1 s/file is 1.5 minutes,
        which is the right cost for "I just deployed and want my
        existing files classified."

        Args:
            force_all: When True (CLI ``bootstrap`` invocation),
                walk every watch unconditionally regardless of the
                per-watch ``bootstrap_existing`` flag. The flag's
                semantics are "auto-run on daemon start"; explicit
                CLI invocation overrides that.
        """
        watches = [
            w for w in self._deps.config.watches.watches if force_all or w.bootstrap_existing
        ]
        if not watches:
            return
        for watch in watches:
            count = 0
            unsorted_root = (watch.destination_root / watch.unsorted_dir).resolve()
            try:
                files = await asyncio.to_thread(
                    _enumerate_existing_files,
                    watch.path,
                    watch.recursive,
                    unsorted_root,
                )
            except OSError as exc:
                _log.warning(
                    "bootstrap_walk_failed",
                    watch=str(watch.path),
                    error=str(exc),
                )
                continue
            _log.info(
                "bootstrap_started",
                watch=str(watch.path),
                file_count=len(files),
            )
            for path in files:
                event = FileEvent(
                    path=path,
                    kind=FileEventKind.ADDED,
                    watch_root=watch.path,
                    destination_root=watch.destination_root,
                    unsorted_dir=watch.unsorted_dir,
                )
                try:
                    await self._on_event(event)
                except Exception as exc:
                    _log.exception(
                        "bootstrap_dispatch_failed",
                        file=str(path),
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    continue
                count += 1
            _log.info(
                "bootstrap_completed",
                watch=str(watch.path),
                files_processed=count,
            )

    async def _recover_orphan_pending(self) -> None:
        """Reconcile pending entries whose ``unsorted_path`` no longer exists.

        Two distinct scenarios produce these orphans:

        - **Crash between ``pending_log.append`` and the move** (see
          the module docstring's crash-window discussion). The
          intended parked path was recorded durably; the move never
          happened. The source file - if it still exists - was at
          ``event.path``, but :class:`PendingDecision` doesn't carry
          that, so we can't redo the move automatically. We
          transition to ``APPLIED`` so the inbound loop doesn't keep
          replaying the now-dead decision_id; the operator can
          recover the source by re-running ``taxonomaid run`` (the
          watcher re-emits it as a fresh event).
        - **User manually deleted the parked file** between park
          time and response time. Same on-disk evidence, same
          recovery: transition to ``APPLIED``.

        This scan runs once at startup, before the watch loop and
        inbound loop start. It does NOT re-issue notifier prompts -
        the operator presumably noticed the parked file vanish (or
        the crash) and will reconcile by hand.
        """
        recovered = 0
        async for pending in self._deps.pending_log.replay():
            if pending.state is not PendingState.REQUESTED:
                continue
            exists = await asyncio.to_thread(self._deps.filesystem.exists, pending.unsorted_path)
            if exists:
                continue
            _log.warning(
                "orphan_pending_recovered",
                decision_id=pending.decision_id,
                unsorted_path=str(pending.unsorted_path),
            )
            await self._deps.pending_log.transition(
                pending.decision_id,
                to=PendingState.APPLIED,
            )
            recovered += 1
        if recovered:
            _log.info("orphan_pending_recovery_summary", recovered=recovered)

    async def _warm_similarity_index(self) -> None:
        if self._deps.similarity is None:
            return
        # Buffer the promotable tail then replay it. The decision log
        # is append-only so the iteration order is chronological;
        # keeping the last ``_SIMILARITY_INDEX_MAX_SAMPLES`` slices off
        # the newest, which carry the most signal for the current
        # taste of the user. ``deque(maxlen=...)`` evicts the oldest
        # entry in O(1) - using ``list.pop(0)`` would be O(n) per
        # eviction, which on a multi-year log past the cap is the
        # difference between a fast restart and a coffee break.
        buffer: deque[Decision] = deque(maxlen=_SIMILARITY_INDEX_MAX_SAMPLES)
        total_seen = 0
        async for decision in self._deps.decision_log.replay():
            if decision.source not in _SIMILARITY_INDEXED_SOURCES:
                continue
            buffer.append(decision)
            total_seen += 1
        for decision in buffer:
            self._deps.similarity.add(
                filename=decision.file.name,
                destination=decision.destination,
            )
        if buffer:
            _log.info(
                "similarity_index_warmed",
                samples=len(buffer),
                cap=_SIMILARITY_INDEX_MAX_SAMPLES,
                evicted_older=max(0, total_seen - len(buffer)),
            )

    async def _watch_loop(self) -> None:
        # Events are processed sequentially: the RecentlyMoved cache
        # and the pending log have ordering semantics (an ADDED for a
        # placed file must record the placement *before* a DELETED for
        # the same path is interpreted as a user override). Bulk
        # imports therefore run at LLM-roundtrip cadence, which is
        # fine for the human-scale workloads this daemon targets and
        # avoids a race that crossed our integration suite.
        async for event in self._deps.watcher.watch():
            try:
                await self._on_event(event)
            except Exception as exc:
                # Catch broadly so a transient OSError on `Path.resolve`
                # or `iterdir` doesn't tear the whole dispatcher down.
                # CancelledError still propagates because Exception
                # excludes BaseException subclasses.
                _log.exception(
                    "dispatch_failed",
                    file=str(event.path),
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

    async def _on_event(self, event: FileEvent) -> None:
        now = self._deps.clock.now()
        if event.kind is FileEventKind.ADDED:
            if self._recent.matches_recent_placement(event.path, now=now):
                return
            await self._handle_event(event)
            return
        if event.kind in (FileEventKind.DELETED, FileEventKind.MODIFIED):
            await self._maybe_record_user_override(event, now=now)

    async def _inbound_loop(self) -> None:
        inbound = self._deps.notifier_inbound
        if inbound is None:
            return
        backoff_s = 1.0
        while True:
            try:
                async for response in inbound.stream():
                    backoff_s = 1.0
                    try:
                        await self._apply_response(response)
                    except TaxonomaidError as exc:
                        _log.error(
                            "notifier_apply_failed",
                            decision_id=response.decision_id,
                            error=str(exc),
                            error_type=type(exc).__name__,
                        )
                # Stream returned without raising: inbound is shutting down.
                return
            except NotifierError as exc:
                _log.warning(
                    "notifier_inbound_reconnecting",
                    error=str(exc),
                    backoff_s=backoff_s,
                )
                await asyncio.sleep(backoff_s)
                backoff_s = min(backoff_s * 2, 60.0)

    async def _handle_event(self, event: FileEvent) -> None:
        await asyncio.sleep(self._deps.debounce_s)
        if not await asyncio.to_thread(self._deps.filesystem.is_file, event.path):
            return

        excerpt = await self._read_excerpt_safely(event.path)

        engine = self._engine_for(event)
        rule_match = engine.match(
            filename=event.path.name,
            ext=event.path.suffix,
            content=excerpt,
        )
        if (
            rule_match.matched
            and rule_match.rule is not None
            and rule_match.destination is not None
        ):
            await self._apply_rule_match(
                event,
                rule=rule_match.rule,
                destination=rule_match.destination,
            )
            return

        # `iterdir`/`is_dir`/`resolve` on a destination tree with
        # thousands of entries can stat hundreds of inodes per event.
        # Push that off the loop so the inbound notifier and other
        # in-flight tasks stay responsive on slow filesystems.
        candidates = await asyncio.to_thread(_candidate_destinations, event)
        prior_user_moves = self._prior_user_moves(event.path.name)

        circuit = self._deps.llm_circuit
        now = self._deps.clock.now()
        if circuit is not None and not circuit.allow(now):
            # Circuit is open and we're inside the cooldown window;
            # skip the LLM call entirely and park silently. The
            # operator already got the "LLM unavailable" alert when
            # the circuit tripped; muting per-file prompts here is
            # the whole point of the circuit.
            circuit.record_skip()
            await self._park_and_notify(
                event,
                response=LLMResponse(
                    destination=Path("_unsorted"),
                    confidence=0.0,
                    reason="LLM circuit open",
                ),
                silent=True,
            )
            return

        try:
            response = await self._deps.llm.classify(
                filename=event.path.name,
                excerpt=excerpt,
                candidate_destinations=candidates,
                prior_user_moves=prior_user_moves,
            )
        except LLMError as exc:
            _log.error("llm_error", file=str(event.path), error=str(exc))
            just_opened = False
            if circuit is not None:
                just_opened = circuit.record_failure(self._deps.clock.now())
                if just_opened:
                    await self._notify_circuit_open(reason=str(exc))
            # Silent when the circuit is now open: either we just
            # tripped (the operator already got the "LLM unavailable"
            # alert above) or we were already open (cooldown probe
            # failed; the storm-prevention window is still in
            # effect). When circuit is None or still CLOSED,
            # surface the per-file prompt - that's the existing
            # one-off-error UX, unchanged.
            silent = circuit is not None and circuit.state is CircuitState.OPEN
            await self._park_and_notify(
                event,
                response=LLMResponse(
                    destination=Path("_unsorted"),
                    confidence=0.0,
                    reason=f"LLM error: {exc}",
                ),
                silent=silent,
            )
            return

        if circuit is not None:
            just_closed = circuit.record_success()
            if just_closed:
                skipped = circuit.take_skipped_count()
                await self._notify_circuit_recovered(skipped_files=skipped)

        thresholds = self._deps.config.llm.thresholds
        if response.confidence >= thresholds.auto_move:
            # Folder-creation gate. The auto_create_folder threshold
            # is necessary but not sufficient: we also require the
            # proposed destination to appear in the candidate set
            # (i.e. the directory already exists, OR a parent
            # directory on the way to it exists). This is the
            # post-validation defence against prompt injection -
            # without it, a hostile filename or excerpt that convinces
            # the LLM to report ``confidence=0.99`` can spawn an
            # arbitrary new folder. With it, the worst a successful
            # injection can do is auto-move into a folder a human
            # has already created.
            may_create = (
                response.confidence >= thresholds.auto_create_folder
                and _destination_is_known(response.destination, candidates)
            )
            await self._auto_move(event, response, may_create_folder=may_create)
            return

        await self._park_and_notify(event, response=response)

    async def _read_excerpt_safely(self, path: Path) -> str:
        """Cap input size and extraction time so pathological files can't DoS us."""
        try:
            size = await asyncio.to_thread(self._deps.filesystem.size, path)
        except TaxonomaidError:
            size = 0
        if size > _MAX_INPUT_BYTES_FOR_EXTRACTION:
            _log.warning(
                "skip_excerpt_oversize",
                file=str(path),
                size=size,
                limit=_MAX_INPUT_BYTES_FOR_EXTRACTION,
            )
            return ""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    read_excerpt,
                    path,
                    max_chars=self._deps.config.llm.max_excerpt_chars,
                ),
                timeout=_EXTRACTION_TIMEOUT_S,
            )
        except TimeoutError:
            _log.warning("excerpt_timeout", file=str(path), timeout_s=_EXTRACTION_TIMEOUT_S)
            return ""

    def _engine_for(self, event: FileEvent) -> RuleEngine:
        if self._deps.rule_engines_by_root is None:
            return self._deps.rule_engine
        return self._deps.rule_engines_by_root.get(event.watch_root, self._deps.rule_engine)

    async def _apply_rule_match(
        self,
        event: FileEvent,
        *,
        rule: Rule,
        destination: Path,
    ) -> None:
        target_dir = safe_resolve(event.destination_root, destination)
        if target_dir is None:
            _log.warning(
                "rule_destination_escaped",
                file=str(event.path),
                rule_id=rule.id,
                proposed=str(destination),
            )
            await self._park_and_notify(
                event,
                response=LLMResponse(
                    destination=Path("_unsorted"),
                    confidence=0.0,
                    reason=f"rule {rule.id} produced a destination outside the watch root",
                ),
            )
            return
        target_path = await self._move_into(target_dir, event.path)

        ts = self._deps.clock.now()
        decision = Decision(
            decision_id=_new_decision_id(),
            ts=ts,
            file=event.path,
            destination=target_dir,
            source=DecisionSource.RULE,
            confidence=rule.confidence,
            rule_id=rule.id,
            reason=f"rule {rule.id} matched",
            features={"weight": rule.weight, "anchored": rule.anchored},
        )
        await self._deps.decision_log.append(decision)
        self._remember_placement(target_path, decision.decision_id, ts)
        self._index_for_similarity(decision)
        _log.info(
            "rule_matched",
            file=str(event.path),
            destination=str(target_dir),
            rule_id=rule.id,
            decision_id=decision.decision_id,
        )

    async def _auto_move(
        self,
        event: FileEvent,
        response: LLMResponse,
        *,
        may_create_folder: bool,
    ) -> None:
        target_dir = safe_resolve(event.destination_root, response.destination)
        if target_dir is None:
            _log.warning(
                "llm_destination_escaped",
                file=str(event.path),
                proposed=str(response.destination),
            )
            await self._park_and_notify(event, response=response)
            return
        target_dir_existed = await asyncio.to_thread(self._deps.filesystem.exists, target_dir)
        if not target_dir_existed and not may_create_folder:
            await self._park_and_notify(event, response=response)
            return

        target_path = await self._move_into(target_dir, event.path)

        ts = self._deps.clock.now()
        decision = Decision(
            decision_id=_new_decision_id(),
            ts=ts,
            file=event.path,
            destination=target_dir,
            source=DecisionSource.LLM,
            confidence=response.confidence,
            reason=_truncate_reason(response.reason),
            features={"folder_created": not target_dir_existed},
        )
        await self._deps.decision_log.append(decision)
        self._remember_placement(target_path, decision.decision_id, ts)
        self._index_for_similarity(decision)
        _log.info(
            "moved",
            file=str(event.path),
            destination=str(target_dir),
            confidence=response.confidence,
            decision_id=decision.decision_id,
        )

    async def _park_and_notify(
        self,
        event: FileEvent,
        *,
        response: LLMResponse,
        silent: bool = False,
    ) -> None:
        # Apply the same containment rule to the ``_unsorted/`` tray as
        # to every other untrusted destination: if the tray is a
        # symlink whose target lives outside the watch root, refuse to
        # park (otherwise we'd silently move the file to wherever the
        # symlink points). Leaves the file in place; the next event
        # will retry, and a human has to fix the tray.
        unsorted_dir = safe_unsorted_dir(event.destination_root, event.unsorted_dir)
        if unsorted_dir is None:
            _log.error(
                "unsorted_dir_escape_refusing_park",
                file=str(event.path),
                destination_root=str(event.destination_root),
                unsorted_dir=str(event.unsorted_dir),
            )
            return

        # Pre-compute the eventual unsorted_path BEFORE writing any
        # persistent state, so the durable ``pending_log`` append below
        # can record the exact path we're about to move to. This is
        # the key correctness step for the crash-window guarantee
        # documented in this module's header docstring.
        unsorted_path = await asyncio.to_thread(
            self._precompute_unsorted_target, unsorted_dir, event.path
        )

        proposed = safe_resolve(event.destination_root, response.destination)
        # On traversal/escape the honest signal is "this file should stay
        # parked": surface unsorted_path.parent so an APPROVE tap is a
        # no-op rather than silently moving the file to the watch root.
        proposed_destination = proposed if proposed is not None else unsorted_path.parent
        # Cap the LLM-derived reason so neither the JSONL audit line
        # nor the suffixed "parked:" wrapper can balloon the log.
        bounded_reason = _truncate_reason(response.reason)
        reason_for_user = (
            bounded_reason
            if proposed is not None
            else (
                f"{bounded_reason} "
                "(LLM proposed a path outside the watch root; tap Approve to keep parked)"
            )
        )
        decision_id = _new_decision_id()
        ts = self._deps.clock.now()

        pending = PendingDecision(
            decision_id=decision_id,
            ts=ts,
            unsorted_path=unsorted_path,
            proposed_destination=proposed_destination,
            destination_root=event.destination_root.resolve(),
            confidence=response.confidence,
            reason=reason_for_user,
        )
        # DURABLE pending append BEFORE the move. If we crash after
        # this and before the move below, ``_recover_orphan_pending``
        # at next startup transitions the orphan entry to APPLIED so
        # the dispatcher doesn't end up with phantom REQUESTED
        # entries.
        await self._deps.pending_log.append(pending)

        # MOVE second. ``shutil.move`` is atomic on same-filesystem
        # renames, so the window between move-start and move-end is
        # one syscall.
        await asyncio.to_thread(self._deps.filesystem.move, event.path, unsorted_path)

        await self._deps.decision_log.append(
            Decision(
                decision_id=decision_id,
                ts=ts,
                file=event.path,
                destination=unsorted_path,
                source=DecisionSource.LLM,
                confidence=response.confidence,
                reason=f"parked: {reason_for_user}",
                features={"proposed_destination": str(proposed_destination)},
            )
        )

        if self._deps.notifier_outbound is not None and not silent:
            try:
                await self._deps.notifier_outbound.notify_pending(
                    decision_id=decision_id,
                    file=unsorted_path,
                    proposed_destination=proposed_destination,
                    confidence=response.confidence,
                    reason=reason_for_user,
                )
            except NotifierError as exc:
                _log.error(
                    "notifier_outbound_failed",
                    decision_id=decision_id,
                    error=str(exc),
                )

        _log.info(
            "parked",
            file=str(event.path),
            unsorted=str(unsorted_path),
            proposed=str(proposed_destination),
            confidence=response.confidence,
            decision_id=decision_id,
        )

    async def _apply_response(self, response: NotifierResponse) -> None:
        # Route review-control responses (``/review``,
        # rule-approve / rule-reject button taps) to the review
        # session before falling through to the per-file path. The
        # ``decision_id`` field is overloaded - for review responses
        # it carries a proposal id, not a pending-decision id, so
        # the per-file lookup below would always miss.
        if response.kind in {
            NotifierResponseKind.REVIEW_START,
            NotifierResponseKind.RULE_APPROVE,
            NotifierResponseKind.RULE_REJECT,
        }:
            await self._handle_review_response(response)
            return

        pending = await self._deps.pending_log.get(response.decision_id)
        if pending is None:
            _log.warning("notifier_unknown_decision", decision_id=response.decision_id)
            return
        if pending.state is PendingState.APPLIED:
            _log.info("notifier_already_applied", decision_id=response.decision_id)
            return

        if response.kind is NotifierResponseKind.REJECT:
            await self._deps.pending_log.transition(
                response.decision_id,
                to=PendingState.ANSWERED,
            )
            _log.info("notifier_rejected", decision_id=response.decision_id)
            return

        target_dir = self._resolve_target(response, pending)
        if target_dir is None:
            return
        # APPROVE on an LLM-escape proposal resolves to unsorted_path.parent
        # (the "stay parked" fallback in _park_and_notify). Detect and
        # short-circuit so we don't rename foo.pdf -> foo (2).pdf for no
        # reason.
        if target_dir == pending.unsorted_path.parent:
            await self._deps.pending_log.transition(
                response.decision_id,
                to=PendingState.ANSWERED,
            )
            _log.info(
                "notifier_kept_parked",
                decision_id=response.decision_id,
                file=str(pending.unsorted_path),
            )
            return

        # Crash-recovery short-circuit: if the parked file is gone the
        # most likely cause is that a previous ``_apply_response``
        # crashed between the move and the ``pending_log.transition``
        # that records it. Without this guard, the replay would attempt
        # ``shutil.move`` on a non-existent source, raise
        # :class:`FileSystemError`, and the inbound loop would log
        # ``notifier_apply_failed`` *forever* (the pending entry never
        # advances out of REQUESTED/ANSWERED). Mark the decision
        # APPLIED so the loop advances; the file is already where the
        # user asked for it to go.
        #
        # This also covers the benign case where the user deleted the
        # parked file by hand before responding to Telegram - we still
        # want the pending entry resolved rather than dangling.
        if not await asyncio.to_thread(
            self._deps.filesystem.exists,
            pending.unsorted_path,
        ):
            _log.warning(
                "notifier_apply_parked_file_missing",
                decision_id=response.decision_id,
                unsorted_path=str(pending.unsorted_path),
                target_dir=str(target_dir),
            )
            await self._deps.pending_log.transition(
                response.decision_id,
                to=PendingState.APPLIED,
            )
            return

        target_path = await self._move_into(target_dir, pending.unsorted_path)

        source = (
            DecisionSource.NOTIFIER_CONFIRMED
            if response.kind is NotifierResponseKind.APPROVE
            else DecisionSource.USER_OVERRIDE
        )
        applied_decision = Decision(
            decision_id=response.decision_id,
            ts=self._deps.clock.now(),
            file=pending.unsorted_path,
            destination=target_dir,
            source=source,
            confidence=1.0,
            reason=_truncate_reason(response.raw_text or response.kind.value),
        )
        await self._deps.decision_log.append(applied_decision)
        self._index_for_similarity(applied_decision)
        await self._deps.pending_log.transition(
            response.decision_id,
            to=PendingState.APPLIED,
        )
        self._remember_placement(target_path, response.decision_id, self._deps.clock.now())
        _log.info(
            "notifier_applied",
            decision_id=response.decision_id,
            destination=str(target_dir),
            kind=response.kind.value,
        )

    async def _handle_review_response(self, response: NotifierResponse) -> None:
        """Drive the rule-review session in response to inbound events.

        Three control kinds:

        - ``REVIEW_START`` (``/review``): send the first pending
          proposal, or a "queue empty" message if there's nothing to
          review.
        - ``RULE_APPROVE`` (button tap): promote the proposal_id to
          ``rules.yaml``, then send the next proposal.
        - ``RULE_REJECT`` (button tap): move the proposal_id to
          ``rejected_rules.yaml``, then send the next proposal.

        Called sequentially with the rest of the inbound stream, so
        no state-machine concurrency to worry about. If the
        operator taps a button after the proposal already moved
        (e.g. the CLI ``taxonomaid review`` was used in parallel),
        the session's idempotent ``approve`` / ``reject`` short-
        circuits and we just send the next proposal.
        """
        outbound = self._deps.notifier_outbound
        review = self._deps.review_session
        if review is None or outbound is None:
            _log.warning(
                "review_response_dropped",
                kind=response.kind.value,
                reason="review_session or outbound unwired",
            )
            return

        # ``RULE_APPROVE`` / ``RULE_REJECT`` apply first, then we
        # always send the next proposal (or completion).
        # ``REVIEW_START`` skips the apply step.
        if response.kind is NotifierResponseKind.RULE_APPROVE:
            applied = await asyncio.to_thread(review.approve, response.decision_id)
            _log.info(
                "review_rule_approved",
                proposal_id=response.decision_id,
                applied=applied is not None,
            )
        elif response.kind is NotifierResponseKind.RULE_REJECT:
            applied = await asyncio.to_thread(review.reject, response.decision_id)
            _log.info(
                "review_rule_rejected",
                proposal_id=response.decision_id,
                applied=applied is not None,
            )

        await self._send_next_review_proposal()

    async def _notify_circuit_open(self, *, reason: str) -> None:
        """Best-effort 'LLM is down' alert. Silent on adapter mismatch."""
        outbound = self._deps.notifier_outbound
        if outbound is None:
            return
        notify = getattr(outbound, "notify_circuit_open", None)
        if notify is None:
            _log.warning(
                "circuit_open_outbound_unsupported",
                outbound=type(outbound).__name__,
            )
            return
        try:
            await notify(reason=reason)
        except NotifierError as exc:
            _log.error("circuit_open_send_failed", error=str(exc))

    async def _notify_circuit_recovered(self, *, skipped_files: int) -> None:
        """Best-effort 'LLM recovered' alert. Silent on adapter mismatch."""
        outbound = self._deps.notifier_outbound
        if outbound is None:
            return
        notify = getattr(outbound, "notify_circuit_recovered", None)
        if notify is None:
            _log.warning(
                "circuit_recovered_outbound_unsupported",
                outbound=type(outbound).__name__,
            )
            return
        try:
            await notify(skipped_files=skipped_files)
        except NotifierError as exc:
            _log.error("circuit_recovered_send_failed", error=str(exc))

    async def _send_next_review_proposal(self) -> None:
        outbound = self._deps.notifier_outbound
        review = self._deps.review_session
        if review is None or outbound is None:
            return
        notify_proposal = getattr(outbound, "notify_rule_proposal", None)
        notify_complete = getattr(outbound, "notify_review_complete", None)
        if notify_proposal is None or notify_complete is None:
            _log.warning(
                "review_outbound_unsupported",
                outbound=type(outbound).__name__,
                reason="adapter does not implement rule-review methods",
            )
            return

        queue = await asyncio.to_thread(review.queue)
        if queue.is_empty:
            try:
                await notify_complete(
                    approved=queue.approved_count,
                    rejected=queue.rejected_count,
                )
            except NotifierError as exc:
                _log.error("review_complete_send_failed", error=str(exc))
            return
        proposal = queue.pending[0]
        try:
            await notify_proposal(
                proposal=proposal,
                sample_filenames=(),  # samples land in proposed_rules.yaml; future enhancement
                index=1,
                total=len(queue.pending),
            )
        except NotifierError as exc:
            _log.error(
                "review_proposal_send_failed",
                proposal_id=proposal.id,
                error=str(exc),
            )

    async def _maybe_record_user_override(
        self,
        event: FileEvent,
        *,
        now: datetime,
    ) -> None:
        decision_id = self._recent.consume_override(event.path, now=now)
        if decision_id is None:
            return
        await self._deps.decision_log.append(
            Decision(
                decision_id=decision_id,
                ts=self._deps.clock.now(),
                file=event.path,
                destination=event.path.parent,
                source=DecisionSource.USER_OVERRIDE,
                confidence=1.0,
                reason=f"user moved {event.path.name} after auto-placement",
                features={"event_kind": event.kind.value},
            )
        )
        _log.info(
            "user_override_detected",
            file=str(event.path),
            decision_id=decision_id,
            event_kind=event.kind.value,
        )

    def _remember_placement(self, path: Path, decision_id: str, ts: datetime) -> None:
        self._recent.remember(path=path, decision_id=decision_id, placed_at=ts)

    async def _move_into(self, target_dir: Path, src: Path) -> Path:
        """``mkdir`` + collision-free target + ``move``, all off the event loop.

        Wrapping these blocking calls in :func:`asyncio.to_thread` keeps
        the dispatcher responsive on slow filesystems (NAS bind-mounts,
        FUSE) where each ``stat`` / ``rename`` can take hundreds of
        milliseconds.
        """
        return await asyncio.to_thread(self._sync_move_into, target_dir, src)

    def _sync_move_into(self, target_dir: Path, src: Path) -> Path:
        self._deps.filesystem.mkdir(target_dir, parents=True, exist_ok=True)
        target_path = collision_free_path(
            target_dir / src.name,
            exists=self._deps.filesystem.exists,
        )
        self._deps.filesystem.move(src, target_path)
        return target_path

    def _precompute_unsorted_target(self, unsorted_dir: Path, src: Path) -> Path:
        """``mkdir`` + collision-free target name, no move.

        Used by :meth:`_park_and_notify` so the durable
        ``pending_log`` append can name the exact path the file will
        live at, before the move actually happens. The two-phase
        sequence (precompute → durable append → move) closes the
        crash window described in this module's header docstring.

        The precomputed name can in principle race with another
        process touching the same directory between the precompute
        and the move; the move itself uses
        :class:`LocalFilesystem.move`'s no-overwrite guard as a
        backstop and raises :class:`FileSystemError` rather than
        clobbering.
        """
        self._deps.filesystem.mkdir(unsorted_dir, parents=True, exist_ok=True)
        return collision_free_path(
            unsorted_dir / src.name,
            exists=self._deps.filesystem.exists,
        )

    def _index_for_similarity(self, decision: Decision) -> None:
        if self._deps.similarity is None:
            return
        if decision.source not in _SIMILARITY_INDEXED_SOURCES:
            return
        self._deps.similarity.add(
            filename=decision.file.name,
            destination=decision.destination,
        )

    def _prior_user_moves(self, filename: str) -> tuple[tuple[str, Path], ...]:
        if self._deps.similarity is None:
            return ()
        return self._deps.similarity.top_matches(filename, limit=self._deps.similarity_top_k)

    def _resolve_target(
        self,
        response: NotifierResponse,
        pending: PendingDecision,
    ) -> Path | None:
        if response.kind is NotifierResponseKind.APPROVE:
            # proposed_destination was already resolved at park time;
            # just verify it still lives under the same destination_root
            # (defends against a tampered pending log).
            proposed_resolved = pending.proposed_destination.resolve()
            try:
                proposed_resolved.relative_to(pending.destination_root.resolve())
            except ValueError:
                _log.warning(
                    "approved_destination_escaped",
                    decision_id=response.decision_id,
                    proposed=str(pending.proposed_destination),
                )
                return None
            return proposed_resolved
        if response.kind is NotifierResponseKind.PROPOSE:
            if response.proposed_destination is None:
                _log.warning(
                    "notifier_propose_missing_path",
                    decision_id=response.decision_id,
                )
                return None
            resolved = safe_resolve(pending.destination_root, response.proposed_destination)
            if resolved is None:
                _log.warning(
                    "user_destination_escaped",
                    decision_id=response.decision_id,
                    proposed=str(response.proposed_destination),
                )
                return None
            return resolved
        return None


def _destination_is_known(proposed: Path, candidates: tuple[Path, ...]) -> bool:
    """Return ``True`` when the LLM's proposal is reachable via known dirs.

    The candidate set is the list of existing directories under the
    watch root (relative paths). A "known" destination either is
    itself in the set OR has a parent in the set (so
    ``Career/CVs/2026`` is accepted when ``Career`` already exists,
    because the auto-move will only need to mkdir the last two
    segments inside an already-curated parent).

    This is the second half of the prompt-injection defence: the LLM
    is told to prefer candidates, and the dispatcher then enforces
    that auto-folder-creation requires landing inside one. Anything
    else falls through to the user-review path.
    """
    if not candidates:
        # No candidates means an empty destination tree; we have no
        # signal either way, so let the confidence gate decide.
        return True
    candidate_strs = {str(c) for c in candidates}
    target = str(proposed)
    if target in candidate_strs:
        return True
    # Walk up the proposed path looking for an existing parent.
    parent = Path(target).parent
    while str(parent) not in {".", ""}:
        if str(parent) in candidate_strs:
            return True
        parent = parent.parent
    return False


def _new_decision_id() -> str:
    return uuid.uuid4().hex[:26]


_CANDIDATE_MAX_DEPTH: int = 3
"""How many levels of nested taxonomy we expose to the LLM.

Walking just the top level forces the model to invent nested paths
every time (e.g. ``Finance/Taxes/2025`` against an existing
``Finance/Taxes/`` is treated as a brand-new folder). Three levels
covers the typical hand-curated layout depth without risking prompt
bloat.
"""

_CANDIDATE_LIMIT: int = 64
"""Hard cap on the number of candidates passed to the LLM.

Bounded so a destination root with thousands of subdirectories doesn't
blow the prompt token budget. Sorted by depth then name so the cap
preferentially keeps shallower (more-likely-relevant) folders.
"""


def _enumerate_existing_files(
    watch_root: Path,
    recursive: bool,
    unsorted_root: Path,
) -> tuple[Path, ...]:
    """Yield regular files under ``watch_root`` for the bootstrap scan.

    Skips:

    - Anything inside ``unsorted_root`` (orphan recovery handles it).
    - Hidden files (``.foo``) and hidden directories.
    - Symlinks (avoids infinite loops; matches the watcher's stance).

    Path resolution avoids :func:`Path.resolve` per entry because
    that hammers the filesystem on a cold cache. The caller already
    resolved ``unsorted_root`` once; we use ``Path.is_relative_to``
    on the raw path under the assumption that the watcher hands
    out paths with the same prefix shape.
    """
    if not watch_root.is_dir():
        return ()
    out: list[Path] = []
    queue: list[Path] = [watch_root]
    while queue:
        directory = queue.pop()
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if not recursive:
                        continue
                    if entry.resolve() == unsorted_root:
                        continue
                    queue.append(entry)
                    continue
                if entry.is_file():
                    out.append(entry)
            except OSError:
                continue
    return tuple(out)


def _candidate_destinations(event: FileEvent) -> tuple[Path, ...]:
    """Existing subdirectories under ``destination_root`` as RELATIVE paths.

    Walks up to :data:`_CANDIDATE_MAX_DEPTH` levels deep so an existing
    ``Finance/Taxes/2025/`` shows up as a candidate (not just
    ``Finance/``). Hidden directories and the watch's ``_unsorted/``
    tray are pruned.

    Returning relative paths is critical: passing absolute paths to the
    LLM leaks the watch root's directory name into responses, which the
    model then redundantly prepends - producing ``watch/watch/CVs/``.
    """
    root = event.destination_root
    if not root.exists():
        return ()
    return tuple(
        _walk_candidate_dirs(
            root,
            unsorted=root / event.unsorted_dir,
            max_depth=_CANDIDATE_MAX_DEPTH,
            limit=_CANDIDATE_LIMIT,
        )
    )


def _walk_candidate_dirs(
    root: Path,
    *,
    unsorted: Path,
    max_depth: int,
    limit: int,
) -> Iterable[Path]:
    root_resolved = root.resolve()
    unsorted_resolved = unsorted.resolve()
    discovered: list[tuple[int, Path]] = []
    # ``deque.popleft`` is O(1); ``list.pop(0)`` would be O(n). Bounded
    # below the noticeable threshold today by ``_CANDIDATE_LIMIT=64``,
    # but the deque keeps the BFS asymptotically clean for free.
    queue: deque[tuple[Path, int]] = deque([(root, 0)])
    # Track resolved targets we've already enqueued so that in-tree
    # symlink cycles (``A -> B`` while ``B -> A``) don't make the BFS
    # revisit forever. The depth/limit caps already bound the total
    # work, but unprotected cycles fill the discovered list with
    # duplicates and bloat the LLM prompt.
    visited: set[Path] = {root_resolved}
    while queue:
        directory, depth = queue.popleft()
        if depth >= max_depth:
            continue
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                # Permission can flip mid-walk on a NAS bind-mount;
                # skip just this entry rather than letting one EACCES
                # abort the whole event.
                continue
            if entry.name.startswith("."):
                continue
            try:
                resolved = entry.resolve()
            except OSError:
                continue
            if resolved == unsorted_resolved:
                continue
            if resolved in visited:
                continue
            visited.add(resolved)
            try:
                relative = resolved.relative_to(root_resolved)
            except ValueError:
                continue
            discovered.append((depth + 1, relative))
            if len(discovered) >= limit:
                yield from (rel for _, rel in discovered)
                return
            queue.append((entry, depth + 1))
    yield from (rel for _, rel in discovered)
