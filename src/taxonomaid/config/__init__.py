"""Typed configuration models and YAML loaders.

Every YAML file is validated against a pydantic model in
:mod:`taxonomaid.config.models`. ``${ENV_VAR}`` placeholders inside the YAML
are resolved against the process environment by
:func:`taxonomaid.config.loader.load_app_config`.
"""

from __future__ import annotations

from taxonomaid.config.loader import (
    load_app_config,
    load_dotenv_file,
    load_rules_file,
    load_yaml_file,
    resolve_file_secrets,
)
from taxonomaid.config.models import (
    AppConfig,
    CoherenceSpecConfig,
    LLMConfig,
    MatchSpecConfig,
    NotifierConfig,
    RuleConfig,
    RulesConfig,
    TelegramConfig,
    Thresholds,
    WatchConfig,
    WatchesConfig,
)
from taxonomaid.config.writer import rule_to_config, write_rules_file

__all__ = [
    "AppConfig",
    "CoherenceSpecConfig",
    "LLMConfig",
    "MatchSpecConfig",
    "NotifierConfig",
    "RuleConfig",
    "RulesConfig",
    "TelegramConfig",
    "Thresholds",
    "WatchConfig",
    "WatchesConfig",
    "load_app_config",
    "load_dotenv_file",
    "load_rules_file",
    "load_yaml_file",
    "resolve_file_secrets",
    "rule_to_config",
    "write_rules_file",
]
