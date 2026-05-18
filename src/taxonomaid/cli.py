"""Typer CLI.

The CLI is a thin shell over services built by
:func:`taxonomaid.bootstrap.build_app_from_paths`. Real subcommand bodies
arrive across Phases 1-5; Phase 0 ships ``doctor`` (config sanity check)
and stubbed ``run``, ``review``, ``audit`` for ergonomics.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from pathlib import Path

import httpx
import typer
from rich.console import Console

from taxonomaid import __version__
from taxonomaid.adapters.decision_log import JsonlDecisionLog
from taxonomaid.adapters.notifiers import TelegramOutbound
from taxonomaid.bootstrap import App, build_app_from_paths
from taxonomaid.config import (
    AppConfig,
    load_app_config,
    load_dotenv_file,
    load_rules_file,
    resolve_file_secrets,
    write_rules_file,
)
from taxonomaid.domain import NotifierError, Rule, TaxonomaidError
from taxonomaid.services import (
    AuditFinding,
    Auditor,
    Miner,
    ProbeResult,
    ProbeStatus,
    ReviewPaths,
    ReviewSession,
    probe_llm,
    probe_telegram,
    probe_telegram_chat_is_private,
)

app = typer.Typer(
    name="taxonomaid",
    help="Hybrid auto-sorter: deterministic rules + LLM fallback that learns over time.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

_console = Console()
_DEFAULT_CONFIG_DIR = Path("config")


def _config_paths(config_dir: Path) -> tuple[Path, Path, Path]:
    return (
        config_dir / "watches.yaml",
        config_dir / "llm.yaml",
        config_dir / "notifier.yaml",
    )


def _load_env(config_dir: Path) -> None:
    """Best-effort secret loader.

    Resolves the operator's secret material in this order (highest
    priority first; subsequent steps only fill gaps):

    1. **Existing process environment** - ``export FOO=...`` from the
       shell, the systemd ``Environment=`` directive, etc.
    2. **``*_FILE`` indirection** - any env var ending in ``_FILE``
       whose value points to a readable file becomes the canonical
       variable (suffix stripped) with the file's contents. Standard
       Docker-secrets convention; let an operator do
       ``GEMINI_API_KEY_FILE=/run/secrets/gemini_api_key`` instead
       of putting the key in plaintext env.
    3. **``config_dir/.env``** (e.g. ``~/.config/taxonomaid/.env``).
       The user-facing "secrets live alongside config files"
       location.
    4. **``config_dir/../.env``** - historical sibling location
       (e.g. ``~/.config/.env``). Kept for backward compatibility,
       but sibling files at this level are shared with other apps
       and therefore risky; the inner ``config_dir/.env`` always
       overrides it.
    5. **``./.env`` in the CWD** - the developer's local override.

    Each layer uses ``override=False`` semantics so anything set by
    a higher-priority step keeps its value.
    """
    # ``*_FILE`` indirection runs BEFORE any dotenv loads so a
    # ``_FILE`` from the container env always wins over a literal
    # value in a `.env` file - which is the right priority for
    # production secrets vs. a developer's local override.
    resolve_file_secrets()
    inside = (config_dir / ".env").resolve()
    sibling = (config_dir / ".." / ".env").resolve()
    cwd_env = (Path.cwd() / ".env").resolve()
    load_dotenv_file(inside)
    if sibling not in {inside, cwd_env}:
        load_dotenv_file(sibling)
    if cwd_env != inside:
        load_dotenv_file()


@app.command()
def version() -> None:
    """Print the package version."""
    _console.print(f"taxonomaid {__version__}")


@app.command()
def doctor(
    config_dir: Path = typer.Option(  # noqa: B008
        _DEFAULT_CONFIG_DIR,
        "--config-dir",
        "-c",
        help="Directory containing watches.yaml, llm.yaml, notifier.yaml.",
    ),
    data_dir: Path = typer.Option(  # noqa: B008
        Path("data"),
        "--data-dir",
        "-d",
        help="Runtime data directory.",
    ),
) -> None:
    """Validate the configuration and exit.

    Loads every YAML file, applies environment interpolation, runs pydantic
    validation, and prints a success summary - or a diagnostic error and
    exit code ``1``.
    """
    _load_env(config_dir)
    watches_path, llm_path, notifier_path = _config_paths(config_dir)
    try:
        app_obj = build_app_from_paths(
            watches_path=watches_path,
            llm_path=llm_path,
            notifier_path=notifier_path,
            data_dir=data_dir,
        )
    except TaxonomaidError as exc:
        _console.print(f"[red]config error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    n_watches = len(app_obj.config.watches.watches)
    _console.print(
        f"[green]config OK[/green]: {n_watches} watch(es), "
        f"LLM model={app_obj.config.llm.model!r}, "
        f"data_dir={app_obj.config.data_dir}",
    )
    _emit_privacy_advisories(app_obj.config)


def _emit_privacy_advisories(config: AppConfig) -> None:
    """Surface non-obvious privacy posture so the operator can opt out.

    Taxonomaid sends filenames and (by default) the first 65 KiB of
    each novel document's text to whatever ``llm.base_url`` resolves
    to. The default is Google Gemini Flash Lite via the OpenAI-compat
    endpoint - a third-party host. Operators running on a personal
    NAS often don't realise this; we surface a one-line advisory so
    the decision is informed. Self-hosted Ollama / vLLM / LocalAI
    endpoints route to localhost and don't trigger the advisory.
    """
    base_url = str(config.llm.base_url).lower()
    is_third_party = not any(host in base_url for host in ("localhost", "127.0.0.1", "[::1]"))
    if is_third_party:
        _console.print(
            "[yellow]privacy notice:[/yellow] LLM endpoint "
            f"{config.llm.base_url} is a third-party host. Filenames and up to "
            f"{config.llm.max_excerpt_chars} characters of file content per "
            "novel document will be transmitted there. For an on-host setup "
            "see Ollama / vLLM / LocalAI in the README."
        )


@app.command()
def run(
    config_dir: Path = typer.Option(  # noqa: B008
        _DEFAULT_CONFIG_DIR,
        "--config-dir",
        "-c",
    ),
    data_dir: Path = typer.Option(  # noqa: B008
        Path("data"),
        "--data-dir",
        "-d",
    ),
    json_logs: bool = typer.Option(
        False,
        "--json-logs/--console-logs",
        help="Emit JSON log records (production) or pretty console output (dev).",
    ),
    log_level: str = typer.Option("INFO", "--log-level", "-L"),
) -> None:
    """Run the dispatcher loop until interrupted."""
    _load_env(config_dir)
    watches_path, llm_path, notifier_path = _config_paths(config_dir)
    try:
        application = build_app_from_paths(
            watches_path=watches_path,
            llm_path=llm_path,
            notifier_path=notifier_path,
            data_dir=data_dir,
            log_json=json_logs,
            log_level=log_level,
        )
    except TaxonomaidError as exc:
        _console.print(f"[red]config error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    try:
        asyncio.run(_run_until_signalled(application))
    except KeyboardInterrupt:
        _console.print("[yellow]interrupted[/yellow]")


@app.command()
def mine(
    config_dir: Path = typer.Option(  # noqa: B008
        _DEFAULT_CONFIG_DIR,
        "--config-dir",
        "-c",
    ),
    data_dir: Path = typer.Option(  # noqa: B008
        Path("data"),
        "--data-dir",
        "-d",
    ),
    min_samples: int = typer.Option(10, "--min-samples"),
    agreement: float = typer.Option(0.90, "--agreement"),
    auto_promote_threshold: float = typer.Option(
        0.97,
        "--auto-promote-threshold",
        help=(
            "Precision required for a proposal to be flagged auto-promotable. "
            "Informational only - mined rules still flow through "
            "`taxonomaid review`."
        ),
    ),
    auto_promote_min_samples: int = typer.Option(
        30,
        "--auto-promote-min-samples",
        help="Minimum sample count for the auto-promotable flag.",
    ),
    notify: bool = typer.Option(
        True,
        "--notify/--no-notify",
        help=(
            "Send a Telegram nudge after mining if the pending review queue "
            "is non-empty. Set --no-notify for silent batch jobs (e.g. when "
            "you're running mine and review back-to-back yourself)."
        ),
    ),
) -> None:
    """Replay the decision log and write candidate rules to ``proposed_rules.yaml``."""
    _load_env(config_dir)
    decision_log_path = data_dir / "decisions.jsonl"
    proposed_path = config_dir / "proposed_rules.yaml"
    rules_path = config_dir / "rules.yaml"
    rejected_path = config_dir / "rejected_rules.yaml"

    if not decision_log_path.exists():
        _console.print(f"[yellow]no decision log at {decision_log_path}[/yellow]")
        raise typer.Exit(code=0)

    existing_rules = _read_optional_rules(rules_path)
    existing_rule_ids = {r.id for r in existing_rules}
    rejected_ids = {r.id for r in _read_optional_rules(rejected_path)}
    existing_proposed_ids = {r.id for r in _read_optional_rules(proposed_path)}

    miner = Miner(
        min_samples=min_samples,
        agreement=agreement,
        auto_promote_threshold=auto_promote_threshold,
        auto_promote_min_samples=auto_promote_min_samples,
    )
    decision_log = JsonlDecisionLog(decision_log_path)

    asyncio.run(
        _run_miner(
            miner,
            decision_log,
            existing_rules,
            existing_proposed_ids | existing_rule_ids,
            rejected_ids,
            proposed_path,
        )
    )

    if notify:
        asyncio.run(_send_review_nudge_if_pending(config_dir))


async def _send_review_nudge_if_pending(config_dir: Path) -> None:
    """Send a Telegram nudge if there are unresolved proposals.

    Best-effort: silent on any kind of error (no Telegram configured,
    network outage, malformed config). The mine output is the
    primary signal; the nudge is convenience.
    """
    review = ReviewSession(ReviewPaths.under(config_dir))
    queue = review.queue()
    if queue.is_empty:
        return

    watches_path, llm_path, notifier_path = _config_paths(config_dir)
    try:
        config = load_app_config(
            watches_path=watches_path,
            llm_path=llm_path,
            notifier_path=notifier_path,
        )
    except TaxonomaidError:
        return

    telegram = config.notifier.telegram
    if telegram is None:
        return

    outbound = TelegramOutbound(
        bot_token=telegram.bot_token.get_secret_value(),
        chat_id=telegram.chat_id,
    )
    try:
        await outbound.notify_review_nudge(pending=len(queue.pending))
    except NotifierError as exc:
        _console.print(
            f"[yellow]review nudge failed:[/yellow] {exc} (mine output above is unaffected)"
        )
    finally:
        await outbound.aclose()


@app.command()
def review(
    config_dir: Path = typer.Option(  # noqa: B008
        _DEFAULT_CONFIG_DIR,
        "--config-dir",
        "-c",
    ),
) -> None:
    """Walk ``proposed_rules.yaml`` and approve / reject each entry interactively."""
    _load_env(config_dir)
    proposed_path = config_dir / "proposed_rules.yaml"
    rules_path = config_dir / "rules.yaml"
    rejected_path = config_dir / "rejected_rules.yaml"

    proposals = _read_optional_rules(proposed_path)
    if not proposals:
        _console.print("No proposals to review.")
        raise typer.Exit(code=0)

    approved_initial = _read_optional_rules(rules_path)
    rejected_initial = _read_optional_rules(rejected_path)

    approved_by_id: dict[str, Rule] = {r.id: r for r in approved_initial}
    rejected_by_id: dict[str, Rule] = {r.id: r for r in rejected_initial}
    pending: list[Rule] = []

    n_approved = 0
    n_rejected = 0
    for rule in proposals:
        _console.print(
            f"\n[bold]{rule.id}[/bold] (confidence={rule.confidence:.2f}, "
            f"samples={rule.sample_count}, weight={rule.weight})"
        )
        _console.print(f"  match: {rule.match}")
        _console.print(f"  destination: {rule.destination_template}")
        choice = typer.prompt("approve / reject / skip [a/r/s]", default="s").strip().lower()
        if choice in {"a", "approve"}:
            approved_by_id[rule.id] = rule
            n_approved += 1
        elif choice in {"r", "reject"}:
            rejected_by_id[rule.id] = rule
            n_rejected += 1
        else:
            pending.append(rule)

    new_approved = tuple(approved_by_id.values())
    new_rejected = tuple(rejected_by_id.values())
    new_pending = tuple(pending)

    # Only rewrite files whose contents actually changed - preserves
    # any manual reordering / formatting of rules.yaml the user did
    # outside the review flow.
    if tuple(approved_initial) != new_approved:
        write_rules_file(rules_path, new_approved)
    if tuple(rejected_initial) != new_rejected:
        write_rules_file(rejected_path, new_rejected)
    if tuple(proposals) != new_pending:
        write_rules_file(proposed_path, new_pending)

    _console.print(
        f"[green]reviewed[/green]: "
        f"{n_approved} approved, {n_rejected} rejected, {len(pending)} skipped",
    )


@app.command()
def health(
    config_dir: Path = typer.Option(  # noqa: B008
        _DEFAULT_CONFIG_DIR,
        "--config-dir",
        "-c",
    ),
) -> None:
    """Probe LLM and Telegram reachability; exit 0 on healthy, 1 otherwise.

    Designed to be called from a Docker / Kubernetes ``HEALTHCHECK``.
    Each probe is bounded to roughly ten seconds (see
    :data:`taxonomaid.services.health._DEFAULT_TIMEOUT_S`) so the CLI
    returns inside the orchestrator's containing timeout. The probes
    do **not** classify any files - they call cheap auth-test
    endpoints (``GET /models`` and ``GET /getMe``).
    """
    _load_env(config_dir)
    watches_path, llm_path, notifier_path = _config_paths(config_dir)
    try:
        config = load_app_config(
            watches_path=watches_path,
            llm_path=llm_path,
            notifier_path=notifier_path,
        )
    except TaxonomaidError as exc:
        _console.print(f"[red]config error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    asyncio.run(_run_health(config))


async def _run_health(config: AppConfig) -> None:
    # Run probes concurrently with a single shared httpx client: a
    # Docker HEALTHCHECK every five minutes benefits from halving the
    # round-trip latency, and reusing the client also halves the TLS
    # handshakes we have to do back-to-back. There's no ordering
    # requirement between the LLM and Telegram probes.
    async with httpx.AsyncClient() as client:
        coros = [
            probe_llm(
                base_url=str(config.llm.base_url),
                api_key=config.llm.api_key.get_secret_value(),
                client=client,
            )
        ]
        if config.notifier.telegram is not None:
            telegram_token = config.notifier.telegram.bot_token.get_secret_value()
            coros.append(probe_telegram(bot_token=telegram_token, client=client))
            coros.append(
                probe_telegram_chat_is_private(
                    bot_token=telegram_token,
                    chat_id=config.notifier.telegram.chat_id,
                    client=client,
                )
            )
        results: list[ProbeResult] = list(await asyncio.gather(*coros))

    for result in results:
        colour = {
            ProbeStatus.OK: "green",
            ProbeStatus.DEGRADED: "yellow",
            ProbeStatus.FAILED: "red",
        }[result.status]
        _console.print(f"[{colour}]{result.status.value}[/{colour}] {result.name}: {result.detail}")

    if not all(r.is_healthy for r in results):
        raise typer.Exit(code=1)


_DEFAULT_DROPIN_PATH = (
    Path.home() / ".config" / "systemd" / "user" / "taxonomaid.service.d" / "readwritepaths.conf"
)


@app.command(name="systemd-paths")
def systemd_paths(
    config_dir: Path = typer.Option(  # noqa: B008
        _DEFAULT_CONFIG_DIR,
        "--config-dir",
        "-c",
    ),
    data_dir: Path = typer.Option(  # noqa: B008
        Path("data"),
        "--data-dir",
        "-d",
    ),
    write_dropin: bool = typer.Option(
        False,
        "--write-dropin",
        help=(
            "Write the directives to a systemd drop-in file at "
            "~/.config/systemd/user/taxonomaid.service.d/readwritepaths.conf "
            "instead of printing to stdout. The drop-in survives "
            "deploy/install.sh reruns."
        ),
    ),
    dropin_path: Path = typer.Option(  # noqa: B008
        _DEFAULT_DROPIN_PATH,
        "--dropin-path",
        help="Override the drop-in destination (default: user-mode XDG path).",
    ),
) -> None:
    """Emit a ``[Service]`` drop-in granting the daemon write access to its roots.

    Reads ``watches.yaml`` and assembles a ``ReadWritePaths=`` directive
    covering the daemon's state directories plus every watch's ``path``
    and ``destination_root``. ``ReadWritePaths=`` is **additive across
    drop-ins** in systemd, so the canonical pattern is to keep the
    shipped unit minimal and add operator paths via a drop-in - the
    drop-in survives ``deploy/install.sh`` reruns; an edit to the
    shipped unit would not.

    Without ``--write-dropin``, prints the block to stdout so you can
    inspect or pipe it. With ``--write-dropin``, writes it directly to
    ``~/.config/systemd/user/taxonomaid.service.d/readwritepaths.conf``;
    re-run after editing ``watches.yaml`` to pick up new roots.
    """
    _load_env(config_dir)
    watches_path, llm_path, notifier_path = _config_paths(config_dir)
    try:
        # Load + validate config only; building the dispatcher (with
        # an HTTP client, similarity index, watcher tasks) just to read
        # `watches[*].destination_root` would be wasteful.
        config = load_app_config(
            watches_path=watches_path,
            llm_path=llm_path,
            notifier_path=notifier_path,
            data_dir=data_dir,
        )
    except TaxonomaidError as exc:
        _console.print(f"[red]config error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    paths: list[Path] = [config_dir.resolve(), data_dir.resolve()]
    for watch in config.watches.watches:
        paths.append(watch.path.resolve())
        paths.append(watch.destination_root.resolve())
    seen: set[Path] = set()
    deduped: list[str] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        deduped.append(str(path))

    block = (
        "# Generated by `taxonomaid systemd-paths --write-dropin`.\n"
        "# Re-run after editing watches.yaml. Drop-ins are additive\n"
        "# across files; rebuild from scratch by deleting this file\n"
        "# before re-running.\n"
        "[Service]\n"
        "ReadWritePaths=" + " ".join(deduped) + "\n"
    )

    if write_dropin:
        try:
            dropin_path.parent.mkdir(parents=True, exist_ok=True)
            dropin_path.write_text(block, encoding="utf-8")
        except OSError as exc:
            _console.print(f"[red]failed to write {dropin_path}:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        _console.print(f"[green]wrote {dropin_path}[/green]")
        _console.print(
            "[yellow]reload systemd to pick up the change:[/yellow] "
            "systemctl --user daemon-reload && "
            "systemctl --user restart taxonomaid.service"
        )
    else:
        # ``typer.echo`` writes plain stdout without Rich's
        # terminal-width-aware line wrapping, so the long
        # ``ReadWritePaths=`` line stays on one line and the output is
        # pipeable into a unit-file fragment without manual fix-up.
        typer.echo(block, nl=False)


@app.command()
def audit(
    config_dir: Path = typer.Option(  # noqa: B008
        _DEFAULT_CONFIG_DIR,
        "--config-dir",
        "-c",
    ),
    data_dir: Path = typer.Option(  # noqa: B008
        Path("data"),
        "--data-dir",
        "-d",
    ),
    min_files: int = typer.Option(4, "--min-files"),
    unsorted_min_files: int = typer.Option(
        5,
        "--unsorted-min-files",
        help=(
            "Threshold above which an `_unsorted/` directory triggers a "
            "backlog finding. Set to 0 to disable the unsorted check."
        ),
    ),
    unsorted_min_age_days: int = typer.Option(
        7,
        "--unsorted-min-age-days",
        help=("Files newer than this don't count toward the unsorted backlog finding."),
    ),
    notify: bool = typer.Option(
        False,
        "--notify/--no-notify",
        help=(
            "Send a Telegram digest when findings exist. Default off "
            "for interactive runs; the systemd timer wires --notify "
            "so weekly findings reach the operator."
        ),
    ),
) -> None:
    """Walk every destination root, report coherence findings, exit 0/1."""
    _load_env(config_dir)
    watches_path, llm_path, notifier_path = _config_paths(config_dir)
    try:
        application = build_app_from_paths(
            watches_path=watches_path,
            llm_path=llm_path,
            notifier_path=notifier_path,
            data_dir=data_dir,
        )
    except TaxonomaidError as exc:
        _console.print(f"[red]config error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    auditor = Auditor(
        min_files=min_files,
        unsorted_min_files=unsorted_min_files,
        unsorted_min_age_days=unsorted_min_age_days,
    )
    findings = auditor.audit(
        [w.destination_root for w in application.config.watches.watches],
        watches=application.config.watches.watches,
    )

    if not findings:
        _console.print("[green]no coherence findings.[/green]")
        return

    for finding in findings:
        _console.print(
            f"[yellow]{finding.kind.value}[/yellow] @ {finding.directory} "
            f"(n={finding.sample_count}): {finding.details}",
        )
        for offender in finding.offenders:
            _console.print(f"    - {offender}")

    if notify:
        asyncio.run(_send_audit_digest(application.config, findings))

    raise typer.Exit(code=1)


async def _send_audit_digest(config: AppConfig, findings: tuple[AuditFinding, ...]) -> None:
    """Best-effort Telegram digest of audit findings.

    Silent on every error class (no Telegram configured, network
    down). The CLI's stdout already carries the full report; the
    Telegram message is the operator's nudge to actually look.
    """
    telegram = config.notifier.telegram
    if telegram is None:
        return
    by_kind: dict[str, list[str]] = {}
    for finding in findings:
        line = f"{finding.directory} (n={finding.sample_count}) - {finding.details}"
        by_kind.setdefault(finding.kind.value, []).append(line)
    outbound = TelegramOutbound(
        bot_token=telegram.bot_token.get_secret_value(),
        chat_id=telegram.chat_id,
    )
    try:
        await outbound.notify_audit_findings(
            findings_by_kind={k: tuple(v) for k, v in by_kind.items()},
        )
    except NotifierError as exc:
        _console.print(
            f"[yellow]audit digest failed:[/yellow] {exc} (stdout output above is unaffected)"
        )
    finally:
        await outbound.aclose()


async def _run_until_signalled(application: App) -> None:
    """Run the dispatcher until SIGINT / SIGTERM, then shut down cleanly.

    On systemd ``ExecStop`` (defaults to SIGTERM) we cancel the
    dispatcher task, await its TaskGroup-style unwind, and then call
    :meth:`App.aclose` so the LLM and Telegram HTTP pools shut down
    gracefully. Without this, a SIGTERM aborts the inbound long-poll
    mid-request and httpx logs an unclosed-connection warning.
    """
    loop = asyncio.get_running_loop()
    dispatch_task = asyncio.create_task(
        application.dispatcher.run(),
        name="taxonomaid.dispatch",
    )

    shutdown_task: asyncio.Task[None] | None = None

    def _on_signal() -> None:
        nonlocal shutdown_task
        if dispatch_task.done() or shutdown_task is not None:
            return
        # Ask adapters to wind down first; the TaskGroup inside
        # `run()` will exit naturally once watcher + inbound stop.
        shutdown_task = asyncio.create_task(
            application.dispatcher.stop(),
            name="taxonomaid.shutdown",
        )

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            # Signal handlers aren't available on every platform
            # (Windows under select event loop). Fall back to default
            # handling there - mostly the dev/test path.
            continue

    try:
        await dispatch_task
    finally:
        # Drain the fire-and-forget shutdown task before closing
        # adapters; an unawaited Task with an unraised exception
        # would otherwise emit "Task exception was never retrieved"
        # to stderr, which is exactly the noise the graceful-shutdown
        # path was trying to suppress. Suppress here because the
        # adapter aclose calls below run regardless.
        if shutdown_task is not None:
            with contextlib.suppress(Exception):
                await shutdown_task
        await application.aclose()


def _read_optional_rules(path: Path) -> tuple[Rule, ...]:
    if not path.exists():
        return ()
    return load_rules_file(path)


async def _run_miner(
    miner: Miner,
    decision_log: JsonlDecisionLog,
    existing_rules: tuple[Rule, ...],
    skip_ids: set[str],
    rejected_ids: set[str],
    proposed_path: Path,
) -> None:
    """Mine proposals; skip ids already approved, proposed, or rejected.

    ``skip_ids`` should be the union of (a) ids already in
    ``rules.yaml`` (so we don't re-propose what's already approved) and
    (b) ids already in ``proposed_rules.yaml`` (so reviewing a stale
    proposal stays a one-step process).
    """
    proposals = await miner.mine(
        decisions=decision_log.replay(),
        existing_rules=existing_rules,
    )
    new_rules = tuple(
        proposal.rule
        for proposal in proposals
        if proposal.rule.id not in rejected_ids and proposal.rule.id not in skip_ids
    )
    write_rules_file(
        proposed_path,
        tuple(_read_optional_rules(proposed_path)) + new_rules,
    )
    _console.print(
        f"[green]mined[/green]: {len(proposals)} proposals; "
        f"{len(new_rules)} added to {proposed_path}",
    )
