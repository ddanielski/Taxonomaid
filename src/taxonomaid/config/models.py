"""Pydantic models that mirror every YAML config file.

Validation failures are raised as :class:`taxonomaid.domain.ConfigError` by
the loader so the daemon refuses to start on malformed input.
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path
from typing import Final

import structlog
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from taxonomaid.config.defaults import (
    DEFAULT_AUTO_CREATE_FOLDER_THRESHOLD,
    DEFAULT_AUTO_MOVE_THRESHOLD,
    DEFAULT_AUTO_PROMOTE_RULE_THRESHOLD,
    DEFAULT_LLM_BASE_URL,
    DEFAULT_LLM_MAX_EXCERPT_CHARS,
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_REQUEST_TIMEOUT_S,
    DEFAULT_NOTIFIER_POLL_TIMEOUT_S,
)
from taxonomaid.domain import (
    CoherenceSpec,
    MatchSpec,
    Rule,
    RuleSource,
)

_log = structlog.get_logger("taxonomaid.config.models")


# Heuristic ReDoS sniff: a quantifier immediately wrapped by another
# quantifier (``(x+)+``, ``(x*)*``, ``(x+)*``, etc.) is the textbook
# exponential-backtracking trigger. ``(a|aa)+`` and similar alternation
# patterns aren't caught here; this is intentionally narrow to keep
# false-positives near zero on hand-authored rules.
_REDOS_NESTED_QUANTIFIER: Final[re.Pattern[str]] = re.compile(r"\([^)]*[+*][^)]*\)\s*[+*]")

# Hard cap on user-supplied regex length. The longest patterns we've
# ever needed for a real classifier rule are ~80 characters; a
# 256-byte ceiling leaves comfortable headroom while ensuring a
# pathological 100 KB regex can't be loaded.
_MAX_FILENAME_REGEX_CHARS: Final[int] = 256


def _looks_redos_prone(pattern: str) -> bool:
    """Return ``True`` when ``pattern`` contains an obvious nested quantifier."""
    return _REDOS_NESTED_QUANTIFIER.search(pattern) is not None


class _StrictModel(BaseModel):
    """Reject unknown fields by default to surface typos as errors."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class WatchConfig(_StrictModel):
    """A single watched root.

    Attributes:
        path: Absolute or project-relative directory to watch.
        recursive: Whether to descend into subdirectories.
        destination_root: Base directory the rule engine and LLM are
            allowed to place files under.
        rules_file: Path to the per-watch rules YAML; ``None`` means the
            watch has no deterministic rules and every file goes through
            the LLM.
        unsorted_dir: Single-segment subdirectory name (under
            ``destination_root``) used as the tray for files awaiting
            user confirmation. Multi-segment paths are rejected so the
            pending-log structural recovery in ``JsonlPendingLog``
            remains valid.
        bootstrap_existing: When True, scan this watch root on daemon
            start and process every existing file as if it had just
            been created. Useful for the first-deploy case: inotify
            only fires on new events, so files already in the watch
            root are otherwise invisible to the daemon. Defaults to
            False so a daemon restart on a busy folder doesn't
            accidentally re-classify thousands of files. Files
            already inside ``unsorted_dir`` are always skipped.
    """

    path: Path
    destination_root: Path
    recursive: bool = True
    rules_file: Path | None = None
    unsorted_dir: Path = Path("_unsorted")
    bootstrap_existing: bool = False

    @field_validator("unsorted_dir")
    @classmethod
    def _single_segment_unsorted(cls, value: Path) -> Path:
        if value.is_absolute() or len(value.parts) != 1:
            msg = (
                f"unsorted_dir must be a single relative segment; got {value!r}. "
                "Multi-segment paths break the pending-log structural recovery."
            )
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _unsorted_dir_not_root_basename(self) -> WatchConfig:
        """Refuse pathological configs where unsorted_dir collides with the root.

        :func:`safe_resolve` (see :mod:`taxonomaid.services.path_safety`)
        strips one redundant copy of ``destination_root.name`` from the
        front of any candidate path. If the operator configures
        ``destination_root: /home/u/_unsorted`` together with
        ``unsorted_dir: _unsorted``, that strip collapses
        ``_unsorted/`` to the empty path, so :func:`safe_unsorted_dir`
        resolves the tray to ``destination_root`` itself - and parked
        files would land at the watch root instead of in the tray.
        """
        if self.unsorted_dir.name == self.destination_root.name:
            msg = (
                f"unsorted_dir {self.unsorted_dir.name!r} cannot share "
                f"its name with the destination_root basename "
                f"({self.destination_root.name!r}); the watch-root-prefix "
                "strip in safe_resolve would collapse the tray to the "
                "destination root itself. Rename one of them."
            )
            raise ValueError(msg)
        return self


class WatchesConfig(_StrictModel):
    """Top-level shape of ``watches.yaml``."""

    watches: tuple[WatchConfig, ...] = Field(default_factory=tuple)

    @field_validator("watches")
    @classmethod
    def _at_least_one(cls, value: tuple[WatchConfig, ...]) -> tuple[WatchConfig, ...]:
        if not value:
            msg = "watches.yaml must declare at least one watch"
            raise ValueError(msg)
        return value


class Thresholds(_StrictModel):
    """Confidence thresholds that gate dispatcher behaviour."""

    auto_move: float = DEFAULT_AUTO_MOVE_THRESHOLD
    auto_create_folder: float = DEFAULT_AUTO_CREATE_FOLDER_THRESHOLD
    auto_promote_rule: float = DEFAULT_AUTO_PROMOTE_RULE_THRESHOLD

    @field_validator("auto_move", "auto_create_folder", "auto_promote_rule")
    @classmethod
    def _bounded(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            msg = f"threshold must be in [0, 1]; got {value!r}"
            raise ValueError(msg)
        return value


class LLMConfig(BaseSettings):
    """OpenAI-compatible LLM provider config.

    Defaults live in :mod:`taxonomaid.config.defaults`. Every field can be
    overridden via:

    1. an explicit value in ``llm.yaml`` (highest priority),
    2. an environment variable prefixed ``TAXONOMAID_LLM_``
       (e.g. ``TAXONOMAID_LLM_MAX_EXCERPT_CHARS=131072``), typically
       loaded from ``.env``, or
    3. the default declared in :mod:`taxonomaid.config.defaults`.

    The historic ``max_excerpt_bytes`` field name is accepted as an
    alias of ``max_excerpt_chars`` (in YAML and as an env var) so
    existing user configs keep working; the unit was always
    characters even when the field was misnamed.
    """

    model_config = SettingsConfigDict(
        extra="forbid",
        frozen=True,
        env_prefix="TAXONOMAID_LLM_",
        env_file=None,
        populate_by_name=True,
    )

    base_url: HttpUrl = HttpUrl(DEFAULT_LLM_BASE_URL)
    model: str = DEFAULT_LLM_MODEL
    # SecretStr ensures accidental ``repr()`` of the config (e.g. into
    # a log line) renders ``SecretStr('**********')`` instead of the
    # raw key. An empty default keeps locally-hosted endpoints (Ollama,
    # vLLM) working without a placeholder env var; the OpenAI-compat
    # adapter still sends an ``Authorization: Bearer`` header that
    # Ollama et al. accept and Gemini/OpenAI reject.
    api_key: SecretStr = SecretStr("")
    request_timeout_s: float = DEFAULT_LLM_REQUEST_TIMEOUT_S
    # ``AliasChoices`` plus ``populate_by_name=True`` lets a YAML file
    # use either ``max_excerpt_chars:`` (canonical) or
    # ``max_excerpt_bytes:`` (deprecated). The corresponding env-var
    # alias (``TAXONOMAID_LLM_MAX_EXCERPT_BYTES``) is wired in
    # :func:`taxonomaid.config.loader._instantiate_settings`, since
    # pydantic-settings doesn't apply ``env_prefix`` to validation
    # aliases.
    max_excerpt_chars: int = Field(
        default=DEFAULT_LLM_MAX_EXCERPT_CHARS,
        validation_alias=AliasChoices("max_excerpt_chars", "max_excerpt_bytes"),
    )
    thresholds: Thresholds = Field(default_factory=Thresholds)

    @property
    def max_excerpt_bytes(self) -> int:
        """Deprecated alias for :attr:`max_excerpt_chars` (same value, characters)."""
        warnings.warn(
            "LLMConfig.max_excerpt_bytes is deprecated; use max_excerpt_chars. "
            "The unit was always characters even when the field was misnamed.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.max_excerpt_chars

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Restrict the source chain to: explicit YAML values, then env vars, then defaults."""
        del settings_cls, dotenv_settings, file_secret_settings
        return (init_settings, env_settings)


class TelegramConfig(_StrictModel):
    """Telegram-specific outbound + inbound credentials.

    Setting both fields enables the direct Bot API outbound (with inline
    keyboards) and the long-polling inbound listener; leaving the
    section absent falls back to Apprise-only outbound and disables
    inbound entirely.

    ``bot_token`` is :class:`SecretStr` so accidental ``repr(config)``
    renders ``SecretStr('**********')`` instead of the raw token.
    """

    bot_token: SecretStr
    chat_id: int


class NotifierConfig(_StrictModel):
    """Outbound + (optional) inbound notifier config.

    ``apprise_urls`` are stored as :class:`SecretStr` because URLs like
    ``tgram://<bot_token>/<chat_id>`` and ``slack://...`` embed
    credentials in the path. The boundary code unwraps them via
    ``.get_secret_value()`` only at the call into Apprise.
    """

    apprise_urls: tuple[SecretStr, ...] = Field(default_factory=tuple)
    telegram: TelegramConfig | None = None
    poll_timeout_s: float = DEFAULT_NOTIFIER_POLL_TIMEOUT_S


class CoherenceSpecConfig(_StrictModel):
    """YAML-side coherence guards, mirrors :class:`CoherenceSpec`."""

    year_match: bool = False

    def to_domain(self) -> CoherenceSpec:
        """Convert to the immutable domain representation."""
        return CoherenceSpec(year_match=self.year_match)


class MatchSpecConfig(_StrictModel):
    """YAML-side match predicate, mirrors :class:`MatchSpec`.

    All fields are optional and conjunctive. ``filename_regex`` is
    pre-compiled at validation time so a malformed regex fails fast.
    """

    filename_regex: str | None = None
    ext: tuple[str, ...] | None = None
    mime_types: tuple[str, ...] | None = None
    content_keywords: tuple[str, ...] | None = None

    @field_validator("filename_regex")
    @classmethod
    def _compile_regex(cls, value: str | None) -> str | None:
        """Validate that the pattern compiles, with a best-effort ReDoS sniff.

        ``re`` has no built-in timeout, and a sound ReDoS guard would
        require the third-party ``regex`` module's ``TIMEOUT`` feature
        or a static structural analyser. We instead do a narrow
        structural check (:func:`_looks_redos_prone`) that catches
        nested quantifiers - the textbook trigger - and **logs a
        warning** without rejecting the rule. The rule corpus is
        still treated as trusted input (hand-authored or human-
        approved via ``taxonomaid review``); the warning is the
        first-line safety net when a marginal pattern slips through
        review. Importing third-party rule files without review
        remains unsafe.
        """
        if value is None:
            return None
        if len(value) > _MAX_FILENAME_REGEX_CHARS:
            msg = (
                f"filename_regex must be <= {_MAX_FILENAME_REGEX_CHARS} chars; "
                f"got {len(value)}. Long patterns are almost always a sign that "
                "the rule wants to be split into several smaller ones, and a "
                "hard cap keeps the engine's pathological cases bounded."
            )
            raise ValueError(msg)
        try:
            re.compile(value)
        except re.error as exc:
            msg = f"invalid filename_regex {value!r}: {exc}"
            raise ValueError(msg) from exc
        if _looks_redos_prone(value):
            _log.warning(
                "filename_regex_possibly_redos_prone",
                pattern=value,
                note=(
                    "nested quantifier detected (e.g. (x+)+, (x*)*, (x+)*); "
                    "this can backtrack exponentially on long inputs. "
                    "Rewrite or anchor the pattern, or accept the risk if "
                    "you trust the input distribution."
                ),
            )
        return value

    @field_validator("ext")
    @classmethod
    def _normalise_ext(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        normalised: list[str] = []
        for ext in value:
            if not ext.startswith("."):
                msg = f"extensions must include the leading dot: got {ext!r}"
                raise ValueError(msg)
            normalised.append(ext.lower())
        return tuple(normalised)

    def to_domain(self) -> MatchSpec:
        """Convert to the immutable domain representation."""
        return MatchSpec(
            filename_regex=self.filename_regex,
            ext=self.ext,
            mime_types=self.mime_types,
            content_keywords=self.content_keywords,
        )


_KNOWN_PLACEHOLDERS: frozenset[str] = frozenset({"year"})
_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


class RuleConfig(_StrictModel):
    """YAML-side rule definition, mirrors :class:`Rule`."""

    id: str = Field(min_length=1)
    match: MatchSpecConfig
    destination_template: str = Field(min_length=1)
    coherence: CoherenceSpecConfig = Field(default_factory=CoherenceSpecConfig)
    weight: float = 1.0
    confidence: float = 1.0
    anchored: bool = False
    source: RuleSource = RuleSource.HAND
    sample_count: int = 0

    @field_validator("destination_template")
    @classmethod
    def _known_placeholders_only(cls, value: str) -> str:
        """Reject ``{quarter}`` / ``{vendor}`` / etc. at config-load time.

        Otherwise the rule loads cleanly but silently fails to fire at
        match time, which is exactly the kind of typo the strict
        config layer is supposed to catch.
        """
        placeholders = set(_TEMPLATE_PLACEHOLDER_RE.findall(value))
        unknown = placeholders - _KNOWN_PLACEHOLDERS
        if unknown:
            msg = (
                f"unknown placeholder(s) in destination_template "
                f"{value!r}: {sorted(unknown)}. "
                f"Currently supported: {sorted(_KNOWN_PLACEHOLDERS)}."
            )
            raise ValueError(msg)
        return value

    @field_validator("confidence")
    @classmethod
    def _bounded_confidence(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            msg = f"confidence must be in [0, 1]; got {value!r}"
            raise ValueError(msg)
        return value

    @field_validator("weight")
    @classmethod
    def _non_negative_weight(cls, value: float) -> float:
        if value < 0.0:
            msg = f"weight must be >= 0; got {value!r}"
            raise ValueError(msg)
        return value

    @field_validator("sample_count")
    @classmethod
    def _non_negative_sample_count(cls, value: int) -> int:
        if value < 0:
            msg = f"sample_count must be >= 0; got {value!r}"
            raise ValueError(msg)
        return value

    def to_domain(self) -> Rule:
        """Convert to the immutable :class:`Rule` domain object."""
        return Rule(
            id=self.id,
            match=self.match.to_domain(),
            destination_template=self.destination_template,
            coherence=self.coherence.to_domain(),
            weight=self.weight,
            confidence=self.confidence,
            anchored=self.anchored,
            source=self.source,
            sample_count=self.sample_count,
        )


class RulesConfig(_StrictModel):
    """Top-level shape of ``rules.yaml`` (and ``proposed_rules.yaml``).

    Stored as a wrapped object so the file can grow non-rule keys later
    (e.g. file-format ``version``) without a breaking change.
    """

    rules: tuple[RuleConfig, ...] = Field(default_factory=tuple)

    @field_validator("rules")
    @classmethod
    def _unique_ids(cls, value: tuple[RuleConfig, ...]) -> tuple[RuleConfig, ...]:
        seen: set[str] = set()
        for rule in value:
            if rule.id in seen:
                msg = f"duplicate rule id: {rule.id!r}"
                raise ValueError(msg)
            seen.add(rule.id)
        return value

    def to_domain(self) -> tuple[Rule, ...]:
        """Convert every rule to its immutable domain form."""
        return tuple(r.to_domain() for r in self.rules)


class AppConfig(_StrictModel):
    """Aggregated config the composition root consumes."""

    watches: WatchesConfig
    llm: LLMConfig
    notifier: NotifierConfig
    data_dir: Path = Path("data")
