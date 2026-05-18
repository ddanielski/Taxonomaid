# Configuration

Every YAML file in `config/` has a corresponding pydantic model.
Loading any file that fails validation raises
[`ConfigError`][taxonomaid.domain.ConfigError] and the daemon refuses
to start.

Secrets are referenced as `${ENV_VAR}` placeholders; the loader
resolves them in two ways:

- `ENV_VAR` itself, read directly from the process environment.
- `ENV_VAR_FILE`, read as a path to a file containing the secret
  (Postgres / MySQL / Redis convention). This is what the Docker
  image, `compose.yaml`, and the systemd unit's `LoadCredential=`
  use, so secrets stay out of `/proc/<pid>/environ`.

Path env vars - `TAXONOMAID_CONFIG_DIR`, `TAXONOMAID_DATA_DIR` -
are honoured by every CLI subcommand as fallbacks for
`--config-dir` / `--data-dir`. The Docker image bakes them in to
`/config` and `/data`.

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

See [`NotifierConfig`][taxonomaid.config.NotifierConfig]. Outbound
delivery follows whatever you configure:

- When `telegram:` is set, the daemon talks **directly** to the
  Telegram Bot API for inline-keyboard approve / reject buttons and
  threaded reply support. Inbound replies use the same connection.
- When `apprise_urls` is set, outbound also fans out through
  **Apprise** to whichever channels you list (Slack, Discord, ntfy,
  email, ...).
- When both are set, a composite outbound dispatches to Telegram
  and the Apprise URLs in parallel; a transient failure on one
  channel doesn't block the others.

## `rules.yaml`

```yaml
--8<-- "config/rules.example.yaml"
```

The rule schema is documented inline. Each rule has a `match`
spec (filename regex / extension / MIME type), a
`destination_template` with `{year}` substitution, optional
`coherence` guards, and a scoring weight. The dispatcher picks the
highest `weight * confidence` rule whose coherence checks pass; if
none pass, control falls through to the LLM.
