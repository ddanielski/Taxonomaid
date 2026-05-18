# Configuration

Every YAML file in `config/` has a corresponding pydantic model. Loading
any file that fails validation raises
[`ConfigError`][taxonomaid.domain.ConfigError] and the daemon refuses to
start. Secrets are referenced as `${ENV_VAR}` placeholders; the loader
resolves them from the process environment.

## `watches.yaml`

```yaml
--8<-- "config/watches.example.yaml"
```

See [`WatchesConfig`][taxonomaid.config.WatchesConfig] and
[`WatchConfig`][taxonomaid.config.WatchConfig] for the full schema.

## `llm.yaml`

```yaml
--8<-- "config/llm.example.yaml"
```

See [`LLMConfig`][taxonomaid.config.LLMConfig] and
[`Thresholds`][taxonomaid.config.Thresholds]. Switching providers is a
config change; any OpenAI-compatible base URL works (Gemini, OpenAI,
Ollama, vLLM, LocalAI, LM Studio, ...).

## `notifier.yaml`

```yaml
--8<-- "config/notifier.example.yaml"
```

See [`NotifierConfig`][taxonomaid.config.NotifierConfig]. Outbound has
two paths today:

- When `telegram:` is set, the daemon talks **directly** to the
  Telegram Bot API to ship inline-keyboard approve/reject buttons and
  threaded reply support. Inbound is wired the same way.
- When `telegram:` is unset, outbound flows through **Apprise** to
  whichever URLs you configure in `apprise_urls`.

Mixing the two ("Telegram direct *and* Apprise fan-out to Slack at the
same time") is a known gap: the bootstrap currently logs a warning and
ignores `apprise_urls` whenever `telegram:` is set. A composite
outbound is a TODO tracked in `bootstrap.py`. For now pick one
delivery mode.

## `rules.yaml`

```yaml
--8<-- "config/rules.example.yaml"
```

The rule schema is documented inline; the matcher and scorer arrive in
Phase 2.
