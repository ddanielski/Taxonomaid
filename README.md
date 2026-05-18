# Taxonomaid

[![CI](https://img.shields.io/github/actions/workflow/status/ddanielski/Taxonomaid/ci.yml?branch=main&label=CI&logo=github)](https://github.com/ddanielski/Taxonomaid/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/github/actions/workflow/status/ddanielski/Taxonomaid/docs.yml?branch=main&label=docs&logo=materialformkdocs)](https://github.com/ddanielski/Taxonomaid/actions/workflows/docs.yml)
[![Docker](https://img.shields.io/github/actions/workflow/status/ddanielski/Taxonomaid/docker.yml?label=docker&logo=docker)](https://github.com/ddanielski/Taxonomaid/actions/workflows/docker.yml)
[![Tests](https://img.shields.io/badge/tests-373%20passed-brightgreen?logo=pytest&logoColor=white)](https://github.com/ddanielski/Taxonomaid/actions/workflows/ci.yml)
[![codecov](https://codecov.io/github/ddanielski/Taxonomaid/graph/badge.svg?token=8TH3J6U3VS)](https://codecov.io/github/ddanielski/Taxonomaid)

[![Version](https://img.shields.io/github/v/tag/ddanielski/Taxonomaid?sort=semver&logo=github&label=version)](https://github.com/ddanielski/Taxonomaid/tags)
[![GHCR image](https://img.shields.io/badge/ghcr.io-ddanielski%2Ftaxonomaid-2496ED?logo=docker&logoColor=white)](https://github.com/ddanielski/Taxonomaid/pkgs/container/taxonomaid)
[![License: MIT](https://img.shields.io/github/license/ddanielski/Taxonomaid?color=brightgreen)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/release/python-3120/)

> Hybrid auto-sorter for shared folders. Cheap deterministic rules first;
> an LLM agent fills the gaps; the LLM's good decisions are mined back into
> rules so the LLM is invoked less and less over time.

## Status

Feature-complete. What's in the box:

- Multi-root dispatcher with rule-first / LLM-fallback flow.
- LLM client for any OpenAI-compatible endpoint (Gemini Flash by
  default; OpenAI, Ollama, vLLM, LocalAI, LM Studio all work).
- Apprise outbound + Telegram inbound notifier.
- Rule engine with anchors, coherence guards, year-template
  substitution.
- Pattern miner with a `taxonomaid review` CLI and Telegram
  `/review` command.
- Feedback loop: recently-moved cache + token-Jaccard filename
  similarity bias (embeddings are a future swap behind the same
  interface).
- Directory auditor.
- Packaging: systemd unit files (`deploy/`) and a Docker / Compose
  setup (`Dockerfile`, `compose.yaml`).

## Quick start

```bash
git clone https://github.com/ddanielski/Taxonomaid.git
cd Taxonomaid
uv sync --all-groups

cp config/watches.example.yaml   config/watches.yaml
cp config/llm.example.yaml       config/llm.yaml
cp config/notifier.example.yaml  config/notifier.yaml

# .env is auto-loaded from the repo root. Shell exports still take
# precedence; pick whichever you prefer.
cat > .env <<'EOF'
GEMINI_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
EOF

uv run taxonomaid doctor    # validates config
uv run taxonomaid run       # main loop
```

> **Pre-existing files.** The watcher uses inotify, which only
> fires on new events — files already in a watch root when the
> daemon starts are otherwise invisible. Three ways to onboard
> them:
>
> - `taxonomaid bootstrap` — one-shot CLI scan; supports `--dry-run`
>   to preview what the LLM would do without moving anything.
> - `bootstrap_existing: true` per-watch flag — same scan, but
>   automatic on daemon startup.
> - `touch <file>` — fake an inotify event for a specific file.

Full instructions live in [`docs/getting-started.md`](docs/getting-started.md)
and the rendered MkDocs site (`uv run mkdocs serve`).

## Telegram setup

The daemon answers via a Telegram bot in a private 1-on-1 chat.
Three values go in `.env`:

```
TELEGRAM_BOT_TOKEN=123456789:AA...
TELEGRAM_CHAT_ID=987654321
```

To get them:

1. **Create the bot.** Open Telegram → message [`@BotFather`](https://t.me/BotFather)
   → `/newbot` → pick a name and a username. BotFather prints a token
   on the form `<numeric_id>:<long-string>`. That's `TELEGRAM_BOT_TOKEN`.
   Treat it like a password.

2. **Start a chat with your new bot.** Search Telegram for the username
   BotFather gave you, open the chat, tap **Start**, and send any
   message (e.g. `hi`). The chat must be initiated by you before the
   bot can reply.

3. **Find the chat ID.** Two easy options:

   - Message [`@userinfobot`](https://t.me/userinfobot); it replies
     with your numeric user ID, which is also your private-chat
     `chat_id`.
   - Or curl `getUpdates` once:
     ```bash
     curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getUpdates"
     ```
     Find `"chat":{"id":987654321,...,"type":"private"}` in the JSON.

4. **Drop both into `.env`** alongside your `GEMINI_API_KEY`.

5. **Verify the wiring.** `taxonomaid health` does a one-shot probe
   against Gemini and the Telegram Bot API and exits non-zero on
   failure:

   ```bash
   uv run taxonomaid health
   ```

   It also warns if your `chat_id` resolves to a group rather than a
   private chat — in a group, every member can press the inline
   approve / reject buttons, which is rarely what you want. Use a
   private chat unless you intentionally want group-wide approvals.

When `telegram:` is set in `notifier.yaml`, the daemon talks to the
Bot API directly (inline keyboard buttons, threaded replies for
custom paths). Non-`tgram://` Apprise URLs (Slack, Discord, ntfy)
configured alongside Telegram are fanned out via a composite
outbound — every channel gets the prompt; a transient outage on one
doesn't tear down the others.

### Reviewing rule proposals on Telegram

Once the miner has accumulated enough decisions, it surfaces
candidate rules to be promoted into the always-fast deterministic
path. Two ways to review them:

- **CLI** (`taxonomaid review`) — full experience, inline regex
  editing, full sample list. Best when you have many proposals at
  once or want to tweak a regex before approving.
- **Telegram** — type `/review` in the chat with your bot. The
  daemon walks the queue one proposal at a time with
  `[✅ Approve]` / `[❌ Reject]` buttons; tapping either applies
  the decision and sends the next proposal. After the last one,
  the bot sends a "review complete" summary.

`taxonomaid mine` (run weekly via the systemd timer or manually)
also sends a one-line nudge — *"📐 You have 3 new rule proposals"*
— when the queue is non-empty and a Telegram outbound is
configured. Pass `--no-notify` for silent batch jobs.

## Production deployment

Two supported targets — pick whichever fits your host.

### Docker Compose

Pre-built multi-arch images (linux/amd64 + linux/arm64) ship to GitHub
Container Registry on every `vX.Y.Z` git tag. API keys go in as
**Docker secrets**; the chat ID is plain env.

```yaml
services:
  taxonomaid:
    # Pin to a specific version tag in production to avoid surprise
    # updates - :latest, :0.1, :0 all silently move under you.
    image: ghcr.io/ddanielski/taxonomaid:0.1.3
    restart: unless-stopped
    # Match the UID/GID that owns the watched + destination folders
    # on the host. ``id <user>`` shows the right values; on most
    # NAS hardware these aren't 1000:1000 (e.g. Synology DSM users
    # often have GID=100 "users"). PUID/PGID come from .env, with
    # 1000:1000 as the fallback so the image is runnable out of
    # the box on a standard Linux desktop.
    user: "${PUID:-1000}:${PGID:-1000}"
    environment:
      TELEGRAM_CHAT_ID: "987654321"
      GEMINI_API_KEY_FILE: /run/secrets/gemini_api_key
      TELEGRAM_BOT_TOKEN_FILE: /run/secrets/telegram_bot_token
    secrets:
      - gemini_api_key
      - telegram_bot_token
    volumes:
      - ./config:/config:rw
      - ./data:/data
      - /volume1/docs:/volume1/docs:rw

secrets:
  gemini_api_key:
    file: ./secrets/gemini_api_key.txt
  telegram_bot_token:
    file: ./secrets/telegram_bot_token.txt
```

The `.env` file alongside `compose.yaml`:

```dotenv
# `id <your-user>` on the host shows these values.
PUID=1000
PGID=100
```

```bash
mkdir -p secrets
printf '%s' '<your-gemini-key>'     > secrets/gemini_api_key.txt
printf '%s' '<your-telegram-token>' > secrets/telegram_bot_token.txt
chmod 600 secrets/*

docker compose pull        # grab the latest image from GHCR
docker compose up -d
docker compose logs -f taxonomaid
```

If you'd rather build from the working tree (e.g. while hacking),
uncomment the `build: .` line in `compose.yaml` and run
`docker compose up -d --build`.

The image follows the standard `*_FILE` convention (Postgres / MySQL /
Redis style): every env var ending in `_FILE` whose value is a
readable file path is consumed at startup and exposed under the
suffix-stripped name.

The image also sets `TAXONOMAID_CONFIG_DIR=/config` and
`TAXONOMAID_DATA_DIR=/data` so subcommands find the right paths
without each invocation needing the `--config-dir` / `--data-dir`
flags. CLI flags still override the env vars when you want them to.
Run one-off commands the usual way:

```bash
docker compose run --rm taxonomaid doctor
docker compose run --rm taxonomaid mine
docker compose run --rm taxonomaid audit
docker compose run --rm taxonomaid review
```

Full Docker notes (UID/GID matrix, healthcheck behaviour, log
rotation) live in [`deploy/README.md`](deploy/README.md).

### systemd user service (any Linux box)

Secrets are kept out of `/proc/<pid>/environ` via systemd's
`LoadCredential=` (v247+), which mounts each secret as a file in
the service's private `$CREDENTIALS_DIRECTORY` tmpfs. The shipped
unit then points the daemon's `*_FILE` env vars at it.

```bash
uv tool install .            # or `pipx install .`
deploy/install.sh            # creates secrets/ + installs unit files

# Drop your secrets into the credential store
printf '%s' '<your-gemini-key>'      > ~/.config/taxonomaid/secrets/gemini_api_key
printf '%s' '<your-telegram-token>'  > ~/.config/taxonomaid/secrets/telegram_bot_token

# Set TELEGRAM_CHAT_ID via a drop-in (the chat ID isn't a credential,
# but it lives outside the YAML)
systemctl --user edit taxonomaid.service
# add:
#   [Service]
#   Environment=TELEGRAM_CHAT_ID=987654321

$EDITOR ~/.config/taxonomaid/watches.yaml

# Add your watched + destination roots to ReadWritePaths= in the unit
# (the daemon runs with ProtectHome=read-only). The CLI prints the
# exact line:
taxonomaid systemd-paths -c ~/.config/taxonomaid \
                         -d ~/.local/share/taxonomaid/data

systemctl --user daemon-reload
systemctl --user enable --now taxonomaid.service
journalctl --user -u taxonomaid.service -f
```

The shipped unit is hardened (`NoNewPrivileges`, `ProtectSystem=strict`,
`MemoryDenyWriteExecute`, empty `CapabilityBoundingSet`, etc.) and
supports TPM-backed encrypted credentials via `LoadCredentialEncrypted=`
on systemd v250.5+. Full details and hardening rationale in
[`deploy/README.md`](deploy/README.md).

### Log rotation

Both decision logs grow monotonically. A logrotate snippet sized for
personal-NAS workloads ships at
[`deploy/logrotate.conf`](deploy/logrotate.conf); edit the paths,
drop it into `/etc/logrotate.d/`, done.

## Running unattended

A few features keep notifications quiet during outages and surface
work that has been quietly piling up:

- **LLM circuit breaker.** Three consecutive LLM errors trip a
  circuit. While it's open, files are parked in `_unsorted/`
  without a per-file Telegram prompt - you get one "LLM
  unavailable" message instead of one per file. When the LLM is
  reachable again, one "back online" message reports how many
  files were parked during the outage. Defaults: 3 failures,
  60 s cooldown.

- **`_unsorted/` backlog finding.** The weekly audit reports
  `_unsorted/` directories where 5+ files have been waiting at
  least 7 days, alongside the existing year-drift and
  category-drift findings.

- **`taxonomaid audit --notify`.** Sends a Telegram digest when
  findings exist; silent otherwise. The shipped audit timer turns
  this on; interactive runs default to stdout-only. Pass
  `--unsorted-min-files 0` to disable the backlog check or tweak
  thresholds with `--unsorted-min-files` /
  `--unsorted-min-age-days`.

## Architecture at a glance

`taxonomaid` is a hexagonal (ports-and-adapters) Python package:

```
domain     pure types, errors                       (no internal imports)
ports      typing.Protocol interfaces               (depends on domain)
services   orchestration                            (depends on ports + domain)
adapters   concrete implementations                 (depends on ports + domain)
bootstrap  composition root                         (depends on everything)
```

The layering is enforced by `import-linter` contracts in
`pyproject.toml`. See [ADR 0001](docs/adr/0001-hexagonal-layering.md) for
the reasoning.

## Development

```bash
uv sync --all-groups
uv run pre-commit install

# Full quality gate (mirrors CI)
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run bandit -r src/taxonomaid -c pyproject.toml
uv run lint-imports
uv run pytest -m "unit or integration" --cov
uv run mkdocs build --strict
```

## License

MIT - see [`LICENSE`](LICENSE).
