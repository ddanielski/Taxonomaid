"""Unit tests for the YAML config loader and pydantic models."""

from __future__ import annotations

import os
import warnings
from pathlib import Path

import pytest

from taxonomaid.config import (
    load_app_config,
    load_dotenv_file,
    load_yaml_file,
    resolve_file_secrets,
)
from taxonomaid.config.defaults import (
    DEFAULT_LLM_MAX_EXCERPT_BYTES,
    DEFAULT_LLM_MAX_EXCERPT_CHARS,
)
from taxonomaid.config.loader import _interpolate_env, _walk_interpolate
from taxonomaid.domain import ConfigError

pytestmark = pytest.mark.unit


def test_interpolate_env_resolves_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOO", "bar")
    assert _interpolate_env("hello-${FOO}") == "hello-bar"


def test_interpolate_env_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_ENV_VAR", raising=False)
    with pytest.raises(ConfigError, match="MISSING_ENV_VAR"):
        _interpolate_env("${MISSING_ENV_VAR}")


def test_walk_interpolate_recurses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X", "value")
    payload = {"k": "${X}", "list": ["${X}", 1, {"nested": "${X}"}]}
    out = _walk_interpolate(payload)
    assert out == {"k": "value", "list": ["value", 1, {"nested": "value"}]}


def test_load_yaml_file_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_yaml_file(tmp_path / "nope.yaml")


def test_load_yaml_file_invalid_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(": : not: yaml :", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_yaml_file(bad)


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_load_app_config_full_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("TG_TOKEN", "12345:abc")

    watches_path = tmp_path / "watches.yaml"
    llm_path = tmp_path / "llm.yaml"
    notifier_path = tmp_path / "notifier.yaml"

    _write(
        watches_path,
        """
watches:
  - path: ./test-watch
    destination_root: ./test-watch
    recursive: true
""",
    )
    _write(
        llm_path,
        """
api_key: ${LLM_API_KEY}
""",
    )
    _write(
        notifier_path,
        """
apprise_urls:
  - tgram://${TG_TOKEN}/100
telegram:
  bot_token: ${TG_TOKEN}
  chat_id: 100
""",
    )

    cfg = load_app_config(
        watches_path=watches_path,
        llm_path=llm_path,
        notifier_path=notifier_path,
    )
    assert cfg.llm.api_key.get_secret_value() == "sk-test"
    assert cfg.llm.thresholds.auto_move == 0.75
    assert cfg.notifier.telegram is not None
    assert cfg.notifier.telegram.chat_id == 100
    assert len(cfg.watches.watches) == 1


def test_load_dotenv_file_loads_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOTENV_LOADER_TEST", raising=False)
    env = tmp_path / ".env"
    env.write_text("DOTENV_LOADER_TEST=hello\n", encoding="utf-8")

    assert load_dotenv_file(env) is True
    assert os.environ["DOTENV_LOADER_TEST"] == "hello"


def test_load_dotenv_file_does_not_override_existing_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOTENV_LOADER_TEST", "from-shell")
    env = tmp_path / ".env"
    env.write_text("DOTENV_LOADER_TEST=from-file\n", encoding="utf-8")

    load_dotenv_file(env)
    assert os.environ["DOTENV_LOADER_TEST"] == "from-shell"


def test_load_dotenv_file_returns_false_when_missing(tmp_path: Path) -> None:
    assert load_dotenv_file(tmp_path / ".env") is False


def test_llm_max_excerpt_chars_uses_default_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TAXONOMAID_LLM_MAX_EXCERPT_CHARS", raising=False)
    monkeypatch.delenv("TAXONOMAID_LLM_MAX_EXCERPT_BYTES", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "x")

    watches_path = tmp_path / "watches.yaml"
    llm_path = tmp_path / "llm.yaml"
    notifier_path = tmp_path / "notifier.yaml"

    _write(
        watches_path,
        """
watches:
  - path: ./test-watch
    destination_root: ./test-watch
""",
    )
    _write(llm_path, "api_key: ${LLM_API_KEY}\n")
    _write(notifier_path, "{}\n")

    cfg = load_app_config(
        watches_path=watches_path,
        llm_path=llm_path,
        notifier_path=notifier_path,
    )
    assert cfg.llm.max_excerpt_chars == DEFAULT_LLM_MAX_EXCERPT_CHARS
    # The legacy ``max_excerpt_bytes`` property still resolves to the
    # same value but emits a DeprecationWarning.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert cfg.llm.max_excerpt_bytes == DEFAULT_LLM_MAX_EXCERPT_BYTES


def test_llm_max_excerpt_chars_env_var_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "x")
    monkeypatch.delenv("TAXONOMAID_LLM_MAX_EXCERPT_BYTES", raising=False)
    monkeypatch.setenv("TAXONOMAID_LLM_MAX_EXCERPT_CHARS", "131072")

    watches_path = tmp_path / "watches.yaml"
    llm_path = tmp_path / "llm.yaml"
    notifier_path = tmp_path / "notifier.yaml"

    _write(
        watches_path,
        """
watches:
  - path: ./test-watch
    destination_root: ./test-watch
""",
    )
    _write(llm_path, "api_key: ${LLM_API_KEY}\n")
    _write(notifier_path, "{}\n")

    cfg = load_app_config(
        watches_path=watches_path,
        llm_path=llm_path,
        notifier_path=notifier_path,
    )
    assert cfg.llm.max_excerpt_chars == 131072


def test_llm_max_excerpt_bytes_env_var_alias_still_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legacy ``TAXONOMAID_LLM_MAX_EXCERPT_BYTES`` env var is still accepted.

    The alias also triggers a :class:`DeprecationWarning` so operators
    see the rename before the alias is dropped.
    """
    monkeypatch.setenv("LLM_API_KEY", "x")
    monkeypatch.delenv("TAXONOMAID_LLM_MAX_EXCERPT_CHARS", raising=False)
    monkeypatch.setenv("TAXONOMAID_LLM_MAX_EXCERPT_BYTES", "131072")

    watches_path = tmp_path / "watches.yaml"
    llm_path = tmp_path / "llm.yaml"
    notifier_path = tmp_path / "notifier.yaml"

    _write(
        watches_path,
        """
watches:
  - path: ./test-watch
    destination_root: ./test-watch
""",
    )
    _write(llm_path, "api_key: ${LLM_API_KEY}\n")
    _write(notifier_path, "{}\n")

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        cfg = load_app_config(
            watches_path=watches_path,
            llm_path=llm_path,
            notifier_path=notifier_path,
        )
    assert cfg.llm.max_excerpt_chars == 131072
    deprecations = [w for w in recorded if issubclass(w.category, DeprecationWarning)]
    assert any("TAXONOMAID_LLM_MAX_EXCERPT_BYTES" in str(w.message) for w in deprecations)


def test_yaml_value_wins_over_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "x")
    monkeypatch.setenv("TAXONOMAID_LLM_MAX_EXCERPT_CHARS", "131072")

    watches_path = tmp_path / "watches.yaml"
    llm_path = tmp_path / "llm.yaml"
    notifier_path = tmp_path / "notifier.yaml"

    _write(
        watches_path,
        """
watches:
  - path: ./test-watch
    destination_root: ./test-watch
""",
    )
    _write(llm_path, "api_key: ${LLM_API_KEY}\nmax_excerpt_chars: 16384\n")
    _write(notifier_path, "{}\n")

    cfg = load_app_config(
        watches_path=watches_path,
        llm_path=llm_path,
        notifier_path=notifier_path,
    )
    assert cfg.llm.max_excerpt_chars == 16384


def test_yaml_legacy_max_excerpt_bytes_alias_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older config that uses ``max_excerpt_bytes:`` keeps loading."""
    monkeypatch.setenv("LLM_API_KEY", "x")
    monkeypatch.delenv("TAXONOMAID_LLM_MAX_EXCERPT_CHARS", raising=False)
    monkeypatch.delenv("TAXONOMAID_LLM_MAX_EXCERPT_BYTES", raising=False)

    watches_path = tmp_path / "watches.yaml"
    llm_path = tmp_path / "llm.yaml"
    notifier_path = tmp_path / "notifier.yaml"

    _write(
        watches_path,
        """
watches:
  - path: ./test-watch
    destination_root: ./test-watch
""",
    )
    _write(llm_path, "api_key: ${LLM_API_KEY}\nmax_excerpt_bytes: 16384\n")
    _write(notifier_path, "{}\n")

    cfg = load_app_config(
        watches_path=watches_path,
        llm_path=llm_path,
        notifier_path=notifier_path,
    )
    assert cfg.llm.max_excerpt_chars == 16384


def test_load_app_config_rejects_empty_watches(tmp_path: Path) -> None:
    watches_path = tmp_path / "watches.yaml"
    llm_path = tmp_path / "llm.yaml"
    notifier_path = tmp_path / "notifier.yaml"

    _write(watches_path, "watches: []\n")
    _write(llm_path, "api_key: x\n")
    _write(notifier_path, "{}\n")

    with pytest.raises(ConfigError, match="at least one"):
        load_app_config(
            watches_path=watches_path,
            llm_path=llm_path,
            notifier_path=notifier_path,
        )


# ---- *_FILE secret indirection (Docker-secrets convention) ----------


def test_resolve_file_secrets_reads_file_into_canonical_var(tmp_path: Path) -> None:
    """A ``GEMINI_API_KEY_FILE`` env var sets ``GEMINI_API_KEY`` from disk."""
    secret = tmp_path / "gemini.txt"
    secret.write_text("AIza-very-secret\n", encoding="utf-8")
    env: dict[str, str] = {"GEMINI_API_KEY_FILE": str(secret)}

    populated = resolve_file_secrets(env=env)

    assert populated == 1
    # Trailing newline stripped so the value is paste-ready.
    assert env["GEMINI_API_KEY"] == "AIza-very-secret"
    # The original ``_FILE`` indirector survives so a re-run is a no-op.
    assert env["GEMINI_API_KEY_FILE"] == str(secret)


def test_resolve_file_secrets_does_not_override_existing_canonical(
    tmp_path: Path,
) -> None:
    """Operator-set canonical var wins over the file indirection.

    A shell ``export GEMINI_API_KEY=foo`` is the strongest signal of
    intent; the ``_FILE`` indirection is a fallback.
    """
    secret = tmp_path / "gemini.txt"
    secret.write_text("from-file", encoding="utf-8")
    env: dict[str, str] = {
        "GEMINI_API_KEY": "from-shell",
        "GEMINI_API_KEY_FILE": str(secret),
    }

    populated = resolve_file_secrets(env=env)

    assert populated == 0
    assert env["GEMINI_API_KEY"] == "from-shell"


def test_resolve_file_secrets_skips_missing_files(tmp_path: Path) -> None:
    """A missing file is logged-and-skipped, not raised.

    A partially-configured runtime (e.g. only Telegram secrets in
    production, no LLM secret in dev) is a reasonable state.
    """
    env: dict[str, str] = {"GEMINI_API_KEY_FILE": str(tmp_path / "does_not_exist")}

    populated = resolve_file_secrets(env=env)

    assert populated == 0
    assert "GEMINI_API_KEY" not in env


def test_resolve_file_secrets_is_generic_across_names(tmp_path: Path) -> None:
    """Any ``*_FILE`` env var is honoured, not just a hard-coded list.

    The convention is broad on purpose - new secret names (e.g.
    ``LLM_API_KEY_FILE``, ``OPENAI_API_KEY_FILE``) work without code
    changes.
    """
    a = tmp_path / "a.txt"
    a.write_text("alpha", encoding="utf-8")
    b = tmp_path / "b.txt"
    b.write_text("bravo", encoding="utf-8")
    env: dict[str, str] = {
        "TELEGRAM_BOT_TOKEN_FILE": str(a),
        "GEMINI_API_KEY_FILE": str(b),
    }

    resolve_file_secrets(env=env)

    assert env["TELEGRAM_BOT_TOKEN"] == "alpha"
    assert env["GEMINI_API_KEY"] == "bravo"


def test_resolve_file_secrets_strips_trailing_newline_only(tmp_path: Path) -> None:
    """Internal whitespace is preserved; only leading/trailing is stripped.

    The Docker-secrets workflow typically writes the secret with a
    trailing newline (``echo "..." > /run/secrets/foo``), so we
    strip it. But a secret like a multiline JWT must keep its
    internal structure intact.
    """
    secret = tmp_path / "key.txt"
    secret.write_text("  hello  world  \n", encoding="utf-8")
    env: dict[str, str] = {"FOO_FILE": str(secret)}
    resolve_file_secrets(env=env)
    assert env["FOO"] == "hello  world"


def test_resolve_file_secrets_ignores_bare_underscore_file(tmp_path: Path) -> None:
    """An env var literally named ``_FILE`` has no canonical name to populate."""
    secret = tmp_path / "x"
    secret.write_text("noop", encoding="utf-8")
    env: dict[str, str] = {"_FILE": str(secret)}
    populated = resolve_file_secrets(env=env)
    assert populated == 0
    assert "" not in env


# ---- Relative rules_file resolution (UX nit fix) --------------------


def _write_full_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    rules_file_value: str,
) -> Path:
    """Write a minimal valid config tree and return ``config_dir``."""
    monkeypatch.setenv("LLM_API_KEY", "x")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    watch_root = tmp_path / "watch"
    watch_root.mkdir()

    (config_dir / "watches.yaml").write_text(
        f"""
watches:
  - path: {watch_root}
    destination_root: {watch_root}
    rules_file: {rules_file_value}
""",
        encoding="utf-8",
    )
    (config_dir / "llm.yaml").write_text("api_key: ${LLM_API_KEY}\n", encoding="utf-8")
    (config_dir / "notifier.yaml").write_text("apprise_urls: []\n", encoding="utf-8")
    return config_dir


def test_relative_rules_file_resolves_against_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare ``rules_file: rules.yaml`` lives next to ``watches.yaml``.

    Reproduces the container UX trap: before the fix, the path was
    resolved against the daemon's CWD, so a config that worked locally
    would silently miss the rules file when run inside a container or
    under a systemd unit that doesn't share the operator's CWD.
    """
    config_dir = _write_full_config(tmp_path, monkeypatch, rules_file_value="rules.yaml")
    cfg = load_app_config(
        watches_path=config_dir / "watches.yaml",
        llm_path=config_dir / "llm.yaml",
        notifier_path=config_dir / "notifier.yaml",
    )
    rules_file = cfg.watches.watches[0].rules_file
    assert rules_file is not None
    assert rules_file == (config_dir / "rules.yaml").resolve()


def test_nested_relative_rules_file_resolves_against_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nested relative path like ``rules/main.yaml`` resolves the same way."""
    config_dir = _write_full_config(tmp_path, monkeypatch, rules_file_value="rules/main.yaml")
    cfg = load_app_config(
        watches_path=config_dir / "watches.yaml",
        llm_path=config_dir / "llm.yaml",
        notifier_path=config_dir / "notifier.yaml",
    )
    rules_file = cfg.watches.watches[0].rules_file
    assert rules_file is not None
    assert rules_file == (config_dir / "rules" / "main.yaml").resolve()


def test_absolute_rules_file_is_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit absolute path bypasses the resolution step.

    The operator wrote ``/srv/shared/rules.yaml`` for a reason - we
    don't second-guess by trying to nudge it toward config_dir.
    """
    shared = tmp_path / "shared"
    shared.mkdir()
    target = (shared / "rules.yaml").resolve()
    config_dir = _write_full_config(tmp_path, monkeypatch, rules_file_value=str(target))
    cfg = load_app_config(
        watches_path=config_dir / "watches.yaml",
        llm_path=config_dir / "llm.yaml",
        notifier_path=config_dir / "notifier.yaml",
    )
    assert cfg.watches.watches[0].rules_file == target


def test_absolute_rules_file_outside_config_dir_does_not_get_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absolute paths are preserved verbatim, not nudged toward config_dir.

    The M11 ``rules_file_outside_config_dir`` warning still fires
    for this case (verified manually via the daemon's stderr output
    when running ``taxonomaid doctor`` against a config with such a
    path), but we don't unit-test the warning emission itself: the
    module's ``_log = structlog.get_logger(...)`` caches the
    processor chain at import time, and ``structlog.testing
    .capture_logs`` can't intercept calls through an already-bound
    logger - making the warning test order-dependent and brittle.
    Pinning the resolution behaviour is what matters; the warning
    is a UX hint that's hard to test without rearchitecting the
    logger plumbing.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = (elsewhere / "rules.yaml").resolve()
    config_dir = _write_full_config(tmp_path, monkeypatch, rules_file_value=str(target))
    cfg = load_app_config(
        watches_path=config_dir / "watches.yaml",
        llm_path=config_dir / "llm.yaml",
        notifier_path=config_dir / "notifier.yaml",
    )
    # The absolute path the operator wrote is preserved exactly.
    assert cfg.watches.watches[0].rules_file == target


def test_relative_rules_file_resolution_independent_of_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolution must NOT depend on the daemon's CWD.

    This is the exact regression the fix targets: before the change,
    different launch directories produced different ``rules_file``
    paths from the same YAML, which broke parity between local
    development and container / systemd deployments.
    """
    config_dir = _write_full_config(tmp_path, monkeypatch, rules_file_value="rules.yaml")

    # Launch from one directory.
    monkeypatch.chdir(tmp_path)
    cfg_a = load_app_config(
        watches_path=config_dir / "watches.yaml",
        llm_path=config_dir / "llm.yaml",
        notifier_path=config_dir / "notifier.yaml",
    )

    # Launch from a completely different directory.
    other_cwd = tmp_path / "different_cwd"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    cfg_b = load_app_config(
        watches_path=config_dir / "watches.yaml",
        llm_path=config_dir / "llm.yaml",
        notifier_path=config_dir / "notifier.yaml",
    )

    assert cfg_a.watches.watches[0].rules_file == cfg_b.watches.watches[0].rules_file
    # And the resolved path is config_dir/rules.yaml, regardless of CWD.
    assert cfg_a.watches.watches[0].rules_file == (config_dir / "rules.yaml").resolve()
