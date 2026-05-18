"""YAML configuration loader with ``${ENV_VAR}`` interpolation.

This is the only module that reads disk in :mod:`taxonomaid.config`; the
pydantic models in :mod:`taxonomaid.config.models` stay pure.
"""

from __future__ import annotations

import os
import re
import warnings
from pathlib import Path
from typing import Any

import structlog
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError
from pydantic_settings import BaseSettings

from taxonomaid.config.models import (
    AppConfig,
    LLMConfig,
    NotifierConfig,
    RulesConfig,
    WatchConfig,
    WatchesConfig,
)
from taxonomaid.domain import ConfigError, Rule

_log = structlog.get_logger("taxonomaid.config.loader")
_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def load_dotenv_file(path: Path | None = None) -> bool:
    """Load a ``.env`` file into the process environment if it exists.

    Variables already present in the environment are **not** overridden,
    so `export FOO=...` from the shell still wins over a `.env` entry.

    Args:
        path: Explicit ``.env`` path. When ``None`` (default), looks for
            ``./.env`` in the current working directory.

    Returns:
        ``True`` when the file existed and was loaded, ``False`` otherwise.
    """
    target = path if path is not None else Path.cwd() / ".env"
    if not target.is_file():
        return False
    load_dotenv(target, override=False)
    return True


_FILE_SUFFIX = "_FILE"


def resolve_file_secrets(*, env: dict[str, str] | None = None) -> int:
    """Resolve ``*_FILE`` env vars by reading their file contents.

    Standard convention used by the official Postgres / MySQL / Redis
    Docker images: any environment variable ending in ``_FILE`` whose
    value points to a readable file is consumed; the file's text
    contents (stripped of trailing whitespace) become the value of
    the same variable name without the suffix.

    Mutates :data:`os.environ` in place by default. Pass an explicit
    ``env`` dict in tests to keep the global namespace untouched.

    Precedence:

    - If the canonical variable is already set (e.g. ``GEMINI_API_KEY``
      came in via the shell), the ``_FILE`` indirection is ignored.
      Operator intent wins.
    - If the file is missing or unreadable, the indirection is
      skipped silently and a single warning is logged. We don't raise
      because a partially-configured runtime (e.g. only Telegram
      secrets in production, no LLM secret in dev) is a reasonable
      state.

    Returns the number of canonical variables that were populated.
    """
    target = env if env is not None else os.environ
    populated = 0
    # ``list(items)`` so we can mutate while iterating.
    for key, value in list(target.items()):
        if not key.endswith(_FILE_SUFFIX):
            continue
        canonical = key[: -len(_FILE_SUFFIX)]
        if not canonical:
            continue
        if canonical in target:
            # Operator already set the canonical name explicitly;
            # don't override that with a file contents.
            continue
        try:
            text = Path(value).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            _log.warning(
                "secret_file_unreadable",
                env_var=key,
                path=value,
                error=str(exc),
            )
            continue
        target[canonical] = text.strip()
        populated += 1
    return populated


def _interpolate_env(value: str) -> str:
    """Replace ``${VAR}`` placeholders in ``value`` with environment values.

    A missing environment variable raises :class:`ConfigError` rather than
    silently substituting an empty string.
    """

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        env_value = os.environ.get(name)
        if env_value is None:
            msg = f"environment variable {name!r} referenced in config is not set"
            raise ConfigError(msg)
        return env_value

    return _ENV_PATTERN.sub(repl, value)


def _walk_interpolate(node: Any) -> Any:
    """Recursively interpolate every string value in a YAML node."""
    if isinstance(node, str):
        return _interpolate_env(node)
    if isinstance(node, dict):
        return {k: _walk_interpolate(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk_interpolate(v) for v in node]
    return node


def load_yaml_file(path: Path) -> Any:
    """Load a YAML file and apply ``${ENV}`` interpolation.

    Args:
        path: Path to the YAML file.

    Returns:
        The parsed Python representation, with all string-valued nodes
        interpolated.

    Raises:
        ConfigError: If the file is missing, unreadable, or invalid YAML.
    """
    if not path.is_file():
        msg = f"config file not found: {path}"
        raise ConfigError(msg)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"failed to read config file {path}: {exc}"
        raise ConfigError(msg) from exc
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        msg = f"invalid YAML in {path}: {exc}"
        raise ConfigError(msg) from exc
    return _walk_interpolate(parsed)


def _validate[M: BaseModel](model_cls: type[M], payload: Any, *, source: Path) -> M:
    try:
        return model_cls.model_validate(payload)
    except ValidationError as exc:
        msg = f"invalid configuration in {source}:\n{exc}"
        raise ConfigError(msg) from exc


_LLM_LEGACY_ENV_ALIASES: dict[str, str] = {
    # Legacy env-var name -> canonical name (without the
    # ``TAXONOMAID_LLM_`` prefix; the prefix is owned by
    # ``LLMConfig.model_config.env_prefix``). pydantic-settings
    # doesn't apply ``env_prefix`` to validation aliases, so we
    # bridge the legacy spelling here at the loader boundary.
    "TAXONOMAID_LLM_MAX_EXCERPT_BYTES": "TAXONOMAID_LLM_MAX_EXCERPT_CHARS",
}


def _bridge_legacy_env_aliases() -> None:
    """Mirror legacy ``TAXONOMAID_*`` env vars to their canonical names.

    Only sets the canonical name when it is unset, so an explicit
    new-name value always wins. Touching ``os.environ`` instead of
    monkey-patching settings sources keeps the bridge invisible to
    consumers of :class:`LLMConfig` and to test fixtures.     Emits a
    :class:`DeprecationWarning` so operators see the rename before
    the alias is dropped in a future release.
    """
    for legacy, canonical in _LLM_LEGACY_ENV_ALIASES.items():
        if legacy in os.environ and canonical not in os.environ:
            warnings.warn(
                f"{legacy} is deprecated; use {canonical}. "
                "The unit was always characters even when the env var was misnamed.",
                DeprecationWarning,
                stacklevel=3,
            )
            os.environ[canonical] = os.environ[legacy]


def _instantiate_settings[S: BaseSettings](
    model_cls: type[S],
    payload: Any,
    *,
    source: Path,
) -> S:
    """Build a pydantic-settings model so env-var fallbacks fire.

    Unlike :meth:`BaseSettings.model_validate`, the constructor walks the
    configured settings sources, so any field missing from ``payload``
    falls back to the matching ``TAXONOMAID_*`` environment variable
    before applying the in-code default.
    """
    if payload is None:
        kwargs: dict[str, Any] = {}
    elif isinstance(payload, dict):
        kwargs = dict(payload)
    else:
        msg = f"top-level YAML in {source} must be a mapping; got {type(payload).__name__}"
        raise ConfigError(msg)
    _bridge_legacy_env_aliases()
    try:
        return model_cls(**kwargs)
    except ValidationError as exc:
        msg = f"invalid configuration in {source}:\n{exc}"
        raise ConfigError(msg) from exc


def load_app_config(
    *,
    watches_path: Path,
    llm_path: Path,
    notifier_path: Path,
    data_dir: Path | None = None,
) -> AppConfig:
    """Load and validate every config file into a single :class:`AppConfig`.

    Args:
        watches_path: Path to ``watches.yaml``.
        llm_path: Path to ``llm.yaml``.
        notifier_path: Path to ``notifier.yaml``.
        data_dir: Override for the runtime data directory. Defaults to
            ``./data``.

    Returns:
        A fully validated :class:`AppConfig`.

    Raises:
        ConfigError: On any IO, parse, env-interpolation, or validation
            error.
    """
    watches_payload = load_yaml_file(watches_path)
    llm_payload = load_yaml_file(llm_path)
    notifier_payload = load_yaml_file(notifier_path)

    watches = _validate(WatchesConfig, watches_payload, source=watches_path)
    llm = _instantiate_settings(LLMConfig, llm_payload, source=llm_path)
    notifier = _validate(NotifierConfig, notifier_payload, source=notifier_path)

    config_dir = watches_path.resolve().parent
    watches = _resolve_relative_rules_files(watches, config_dir=config_dir)
    _warn_rules_files_outside_config_dir(watches, config_dir=config_dir)

    return AppConfig(
        watches=watches,
        llm=llm,
        notifier=notifier,
        data_dir=data_dir if data_dir is not None else Path("data"),
    )


def _resolve_relative_rules_files(watches: WatchesConfig, *, config_dir: Path) -> WatchesConfig:
    """Resolve relative ``rules_file`` paths against the config directory.

    A line like ``rules_file: rules.yaml`` in ``watches.yaml`` means
    "the rules file lives next to this watches.yaml" - which is what
    every config file in the world implicitly means by a relative
    path. ``Path("rules.yaml")`` on its own resolves against the
    daemon's CWD, which is fine for ``cd ~/Taxonomaid && uv run``
    but breaks under containers (``WORKDIR /app`` makes
    ``Path("config/rules.yaml")`` resolve to ``/app/config/rules.yaml``,
    not the bind-mounted ``/config``) and breaks under systemd
    (``WorkingDirectory=`` defaults to ``/``).

    By resolving relative paths against ``config_dir`` (the parent
    of ``watches.yaml``) at load time, the same YAML works
    identically whether the operator launches from the repo root, a
    container, or a systemd unit. Absolute paths are left alone -
    they're an explicit override and the loader respects the
    operator's intent.

    The config schema doesn't change; this is a pure normalisation
    step inside the loader.
    """
    new_watches: list[WatchConfig] = []
    changed = False
    for watch in watches.watches:
        if watch.rules_file is None or watch.rules_file.is_absolute():
            new_watches.append(watch)
            continue
        resolved = (config_dir / watch.rules_file).resolve()
        new_watches.append(watch.model_copy(update={"rules_file": resolved}))
        changed = True
    if not changed:
        return watches
    return watches.model_copy(update={"watches": tuple(new_watches)})


def _warn_rules_files_outside_config_dir(watches: WatchesConfig, *, config_dir: Path) -> None:
    """Log a warning when an *absolute* ``rules_file`` escapes the config dir.

    Relative ``rules_file`` paths are resolved against the config
    directory by :func:`_resolve_relative_rules_files`, so by the time
    we reach this check every relative path is already inside.
    The only way to trip this warning today is to write an explicit
    absolute path that points elsewhere. That's a legitimate choice
    for a multi-tenant deployment with a shared rule store, but
    far more often it's a typo or a stale copy-paste from another
    machine; a one-line warning lets the operator notice.
    """
    config_dir_resolved = config_dir.resolve()
    for watch in watches.watches:
        if watch.rules_file is None:
            continue
        try:
            watch.rules_file.resolve().relative_to(config_dir_resolved)
        except ValueError:
            _log.warning(
                "rules_file_outside_config_dir",
                watch=str(watch.path),
                rules_file=str(watch.rules_file),
                config_dir=str(config_dir_resolved),
            )


def load_rules_file(path: Path) -> tuple[Rule, ...]:
    """Load and validate a ``rules.yaml`` file.

    Args:
        path: Path to the YAML file.

    Returns:
        An immutable, validated tuple of :class:`Rule` objects, ready to
        be handed to :class:`taxonomaid.services.RuleEngine`.

    Raises:
        ConfigError: On any IO, parse, env-interpolation, or validation
            failure (including duplicate rule IDs and malformed regexes).
    """
    payload = load_yaml_file(path)
    if payload is None:
        return ()
    config = _validate(RulesConfig, payload, source=path)
    return config.to_domain()
