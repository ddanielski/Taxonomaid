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

```bash
docker build -t taxonomaid:latest .

# Edit ./config/{watches,llm,notifier,rules}.yaml and create a .env file
# with GEMINI_API_KEY (and optionally TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).

docker compose up -d
docker compose logs -f taxonomaid
```

The image uses `python:3.12-slim`, runs as `uid=1000`, declares
`/config` and `/data` as volumes, and ships a `HEALTHCHECK` that
re-validates the configuration via `taxonomaid doctor` every five
minutes - so misconfigurations surface in `docker ps`.

One-off operations:

```bash
docker compose run --rm taxonomaid doctor
docker compose run --rm taxonomaid mine
docker compose run --rm taxonomaid audit
docker compose run --rm taxonomaid review
```
