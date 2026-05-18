# Deployment

Two supported targets, in order of how the project is meant to be
adopted:

1. [systemd user service](#systemd-user-service) for a single-user
   Linux machine.
2. [Docker Compose](#docker-compose) for a NAS or any container host.

Both share the same on-disk layout - one config directory plus one data
directory - so switching between the two is a path change, not a
re-install.

## systemd user service

```bash
uv tool install .         # or `pipx install .`
deploy/install.sh
$EDITOR ~/.config/taxonomaid/env
$EDITOR ~/.config/taxonomaid/watches.yaml

taxonomaid doctor -c ~/.config/taxonomaid

systemctl --user enable --now taxonomaid.service
systemctl --user enable --now taxonomaid-mine.timer    # optional, weekly
systemctl --user enable --now taxonomaid-audit.timer   # optional, weekly

journalctl --user -u taxonomaid.service -f
```

`install.sh` is idempotent and never overwrites existing config files
or the `env` file. The shipped unit files include standard hardening
(`NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict`,
`ProtectHome=read-only` with explicit `ReadWritePaths=`).

If you watch directories outside `${HOME}` (e.g. a NAS bind-mount under
`/srv` or `/mnt`), add them to `ReadWritePaths=` in
`~/.config/systemd/user/taxonomaid.service` and run
`systemctl --user daemon-reload`.

## Docker Compose

Pre-built multi-arch images (`linux/amd64` + `linux/arm64`) are
published to GHCR on every `vX.Y.Z` git tag at
`ghcr.io/ddanielski/taxonomaid`. To build from a local checkout
instead, uncomment the `build: .` line in `compose.yaml` and run
`docker compose up -d --build`.

```bash
# Drop secrets in:
mkdir -p secrets
printf '%s' '<gemini-key>'      > secrets/gemini_api_key.txt
printf '%s' '<telegram-token>'  > secrets/telegram_bot_token.txt
chmod 600 secrets/*

docker compose pull
docker compose up -d
docker compose logs -f taxonomaid
```

The image uses `python:3.12-slim`, runs as `uid=1000`, declares
`/config` and `/data` as volumes, and ships a `HEALTHCHECK` that
runs `taxonomaid health` (probes the LLM endpoint and the Telegram
Bot API) every five minutes, so transient outages surface in
`docker ps`.

### Path env vars

The image sets two env vars so subcommands find the right paths
without each invocation having to repeat `--config-dir /config
--data-dir /data`:

| Env var | Default in the image | CLI flag (wins over env) |
|---|---|---|
| `TAXONOMAID_CONFIG_DIR` | `/config` | `--config-dir`, `-c` |
| `TAXONOMAID_DATA_DIR` | `/data` | `--data-dir`, `-d` |

These work outside Docker too - export them in your shell or
systemd unit and every subcommand picks them up.

### One-off operations

```bash
docker compose run --rm taxonomaid doctor
docker compose run --rm taxonomaid mine
docker compose run --rm taxonomaid audit
docker compose run --rm taxonomaid review
```
