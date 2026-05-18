# Deployment

Two supported targets:

1. **systemd user service** (Phase 6). Runs Taxonomaid as a long-lived
   user-level daemon, with two optional timer-driven companions for the
   miner and the auditor.
2. **Docker** (Phase 7). Image build + sample `compose.yaml` for the NAS.

## systemd user service

Quickest path:

```bash
# from the repo root
uv tool install .            # or `pipx install .`
deploy/install.sh

# Drop your secrets into the credential store
printf '%s' '<your-gemini-key>'      > ~/.config/taxonomaid/secrets/gemini_api_key
printf '%s' '<your-telegram-token>'  > ~/.config/taxonomaid/secrets/telegram_bot_token

# Set TELEGRAM_CHAT_ID via a drop-in (it's not a credential, but
# the unit needs it as an env var). The drop-in survives package
# upgrades; the shipped unit doesn't.
systemctl --user edit taxonomaid.service
# add:
#   [Service]
#   Environment=TELEGRAM_CHAT_ID=987654321

$EDITOR ~/.config/taxonomaid/watches.yaml

taxonomaid doctor -c ~/.config/taxonomaid
systemctl --user enable --now taxonomaid.service

# optional companions
systemctl --user enable --now taxonomaid-mine.timer
systemctl --user enable --now taxonomaid-audit.timer

# inspect
journalctl --user -u taxonomaid.service -f
```

`install.sh` is idempotent: re-running it never overwrites existing
config files or the secret files.

### Secrets via systemd's credential store

The shipped service units use **`LoadCredential=`** rather than
`EnvironmentFile=` for the Gemini API key and the Telegram bot
token. The difference matters:

- `EnvironmentFile=` puts every variable into the service's
  environment, which means they show up in `/proc/<pid>/environ`.
  Any other process running as the same user can read that file.
- `LoadCredential=NAME:PATH` reads the file once at service start
  and exposes it as `$CREDENTIALS_DIRECTORY/NAME` on a tmpfs that
  is **only** readable by the running service. The variable never
  enters `environ`.

The Taxonomaid daemon already supports the `*_FILE` indirection
convention (Postgres / MySQL / Redis style), so the unit ties the
two together:

```ini
LoadCredential=gemini_api_key:%h/.config/taxonomaid/secrets/gemini_api_key
LoadCredential=telegram_bot_token:%h/.config/taxonomaid/secrets/telegram_bot_token

Environment=GEMINI_API_KEY_FILE=%d/gemini_api_key
Environment=TELEGRAM_BOT_TOKEN_FILE=%d/telegram_bot_token
```

`%d` expands to `$CREDENTIALS_DIRECTORY` (typically
`/run/credentials/taxonomaid.service/`). The on-disk files at the
`LoadCredential=` source paths still need filesystem-level
protection - `install.sh` creates them at `chmod 600` inside a
`chmod 700` directory.

`LoadCredential=` requires **systemd v247+** (late 2020). On older
hosts, swap the four directives above for the legacy single line:

```ini
EnvironmentFile=-%h/.config/taxonomaid/env
```

…and put `GEMINI_API_KEY=...` / `TELEGRAM_BOT_TOKEN=...` /
`TELEGRAM_CHAT_ID=...` in that file (chmod 600).

#### TPM-backed encrypted credentials (optional, v250.5+)

If you want the on-disk secret encrypted at rest - useful when the
host's home directory is part of an encrypted volume that's
mounted while you're logged in but readable to other root tools -
use `systemd-creds`:

```bash
# Encrypt once (TPM2 if available; falls back to host-bound key).
systemd-creds encrypt --name=gemini_api_key - \
    ~/.config/taxonomaid/secrets/gemini_api_key.cred \
    <<< 'AIza-real-key-here'
chmod 600 ~/.config/taxonomaid/secrets/gemini_api_key.cred
```

Then in the unit (drop-in via `systemctl --user edit
taxonomaid.service`):

```ini
[Service]
LoadCredentialEncrypted=gemini_api_key:%h/.config/taxonomaid/secrets/gemini_api_key.cred
```

Same `Environment=GEMINI_API_KEY_FILE=%d/gemini_api_key` line as
the unencrypted variant. systemd decrypts at service start; the
plaintext only ever lives in the tmpfs.

### Files installed

- `~/.config/taxonomaid/{watches,llm,notifier,rules}.yaml` (copied from
  `config/*.example.yaml` on first install).
- `~/.config/taxonomaid/secrets/{gemini_api_key,telegram_bot_token}`
  (mode `0600` inside a `0700` directory) - read once at service
  start via `LoadCredential=`.
- `~/.local/share/taxonomaid/data/` - runtime state
  (`decisions.jsonl`, `pending_decisions.jsonl`).
- `~/.config/systemd/user/taxonomaid.service` - the daemon.
- `~/.config/systemd/user/taxonomaid-mine.{service,timer}` - weekly
  pattern miner (Mon 03:30 with up to 10 min of jitter).
- `~/.config/systemd/user/taxonomaid-audit.{service,timer}` - weekly
  directory auditor (Sun 04:00 with up to 10 min of jitter).

### Hardening

The unit files declare standard hardening:

- `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict`,
  `ProtectHome=read-only`.
- A defence-in-depth bundle (`ProtectKernelTunables`,
  `ProtectKernelModules`, `RestrictNamespaces`,
  `MemoryDenyWriteExecute`, `SystemCallFilter=@system-service`,
  empty `CapabilityBoundingSet`, ...). All free for a pure-Python
  daemon.
- `ReadWritePaths=` for the daemon's own state dirs only. The
  operator's watched + destination roots are added via a drop-in.

> **Important.** With `ProtectSystem=strict` + `ProtectHome=read-only`
> the daemon **cannot move files** anywhere unless that path is
> listed in `ReadWritePaths=`. Each watch's `path` and
> `destination_root` **must** appear or every move will fail with
> `EPERM`.

The right pattern is a systemd drop-in: `ReadWritePaths=` is
**additive across drop-ins**, so the shipped unit stays
operator-agnostic and the operator-specific roots live in a
file that survives `install.sh` reruns. Generate the drop-in
with:

```bash
taxonomaid systemd-paths --write-dropin \
    -c ~/.config/taxonomaid \
    -d ~/.local/share/taxonomaid/data
```

This writes
`~/.config/systemd/user/taxonomaid.service.d/readwritepaths.conf`
with a complete `[Service]` block:

```ini
# Generated by `taxonomaid systemd-paths --write-dropin`.
[Service]
ReadWritePaths=/home/you/.config/taxonomaid /home/you/.local/share/taxonomaid/data /home/you/Documents
```

Then reload:

```bash
systemctl --user daemon-reload
systemctl --user restart taxonomaid.service
```

Re-run `--write-dropin` whenever you edit `watches.yaml`. Without
the flag, the same block is printed to stdout - useful for piping
into `systemctl --user edit` or auditing what would change.

Alternatively, relax `ProtectHome=` to `tmpfs` if you'd rather not
maintain the allow-list.

## Uninstalling

```bash
deploy/uninstall.sh           # remove units; keep config + data + secrets
deploy/uninstall.sh --purge   # also remove ~/.config/taxonomaid
                              # AND ~/.local/share/taxonomaid
```

The script stops + disables every Taxonomaid unit, removes the
unit files and the drop-in directory, then reloads the user
daemon. Without `--purge` it leaves your config, secrets, and
runtime state on disk - safer default for "I want to clean up the
plumbing without losing my mined rules and decision history."

The `taxonomaid` executable itself is left in place; remove it with
`pipx uninstall taxonomaid` or `uv tool uninstall taxonomaid`.

## System-service variant (root-installed)

The shipped units are user-mode services (`systemctl --user`,
`~/.config/systemd/user/`). That's the recommended posture for a
single-operator NAS: the daemon runs as you, no privilege gaps,
no `sudo` needed for routine operations.

For multi-tenant hosts or service-account deployments, run the
daemon as a **system service** under a dedicated unprivileged
account. Steps:

1. **Create the account** (no shell, no home directory):

   ```bash
   sudo useradd --system --user-group --shell /usr/sbin/nologin \
                --home-dir /var/lib/taxonomaid taxonomaid
   sudo install -d -o taxonomaid -g taxonomaid -m 0750 \
       /var/lib/taxonomaid \
       /etc/taxonomaid \
       /etc/taxonomaid/secrets
   sudo chmod 0700 /etc/taxonomaid/secrets
   ```

2. **Install the binary** somewhere on `PATH` (e.g. `pipx install
   --system .` or `uv tool install --user-prefix /usr/local .`).

3. **Adapt the unit file**:

   - Copy `deploy/systemd/taxonomaid.service` to
     `/etc/systemd/system/taxonomaid.service`.
   - Replace `%h` with absolute paths (`/etc/taxonomaid`,
     `/var/lib/taxonomaid`).
   - Add `User=taxonomaid` and `Group=taxonomaid` under
     `[Service]`.
   - Adjust `ExecStart=` to the absolute path of the installed
     `taxonomaid` binary (e.g. `/usr/local/bin/taxonomaid`).
   - Drop the `ProtectHome=read-only` line - the service account
     has no home to protect; `ProtectSystem=strict` plus the
     allow-listed `ReadWritePaths=` is sufficient.

4. **Same secrets workflow**, with the secret files owned by
   `taxonomaid:taxonomaid` mode `0600`:

   ```bash
   sudo install -o taxonomaid -g taxonomaid -m 0600 /dev/null \
       /etc/taxonomaid/secrets/gemini_api_key
   echo -n '<key>' | sudo tee /etc/taxonomaid/secrets/gemini_api_key >/dev/null
   ```

   The `LoadCredential=` directive then reads them as the service
   user; the source files don't need to be readable by anyone
   else.

5. **Generate the drop-in** for system mode:

   ```bash
   sudo -u taxonomaid taxonomaid systemd-paths --write-dropin \
       --dropin-path /etc/systemd/system/taxonomaid.service.d/readwritepaths.conf \
       -c /etc/taxonomaid \
       -d /var/lib/taxonomaid
   ```

6. **Enable**:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now taxonomaid.service
   sudo journalctl -u taxonomaid.service -f
   ```

The user-service path remains the recommended default - the
system-service variant exists for shared hosts where the operator
isn't the only user and root-level isolation matters.

## Docker

The repo ships a [`Dockerfile`](../Dockerfile) (multi-stage uv-based
build), a sample [`compose.yaml`](../compose.yaml), and a GitHub
Actions workflow that publishes pre-built multi-arch images
(linux/amd64 + linux/arm64) to GitHub Container Registry on every
`vX.Y.Z` git tag.

### Production: pull from GHCR

`compose.yaml` ships configured for the registry image, so the
NAS-side workflow is just:

```bash
# Edit ./config/{watches,llm,notifier,rules}.yaml.
# Drop your secrets into ./secrets/ (see "Secrets" below).
# Set TELEGRAM_CHAT_ID in your shell or in a `.env` file.

docker compose pull
docker compose up -d
docker compose logs -f taxonomaid
```

### Development: build from source

For iterating on the Dockerfile or testing un-tagged commits,
uncomment `build: .` in `compose.yaml` (Compose prefers a local
build when `build:` is set):

```bash
docker compose up -d --build
```

### Image tags

The publish workflow emits these tags on a `vX.Y.Z` push:

| Source ref | Tags pushed |
|---|---|
| `vX.Y.Z` (stable) | `X.Y.Z`, `X.Y`, `X`, `latest` |
| `vX.Y.Z-rc1` (prerelease) | `X.Y.Z-rc1`, `X.Y`, `X` (NOT `latest`) |
| Manual `workflow_dispatch` | `edge` (or whatever `tag` input you set; never `latest`) |

In production, pin to `:X.Y.Z` so a fresh `latest` push doesn't
silently update your daemon. `:X.Y` lets you ride patch releases
within a minor; `:latest` is fine for "always on the newest" but
loses you the version-bump signal.

> **One-time setup**: the first push from this workflow creates the
> package as **private**. Visit
> `https://github.com/users/<you>/packages/container/taxonomaid/settings`
> and flip "Package visibility" to public if you want
> unauthenticated pulls (the typical setup for a homelab tool).

The image:

- Uses Python 3.12 on `python:3.12-slim`.
- Runs as an unprivileged user (`uid=1000`); see "UID/GID matching"
  below before bind-mounting host folders.
- Declares `/config` and `/data` as volumes.
- Defines a `HEALTHCHECK` that runs `taxonomaid health` every five
  minutes - probes both the LLM endpoint and Telegram and reports
  unhealthy in `docker ps` if either is unreachable / unauthorized.

### Secrets

The shipped `compose.yaml` passes the Gemini API key and the
Telegram bot token as **Docker secrets** rather than environment
variables. The chat ID is plaintext env (it isn't a credential -
knowing it doesn't let an attacker do anything without the bot
token).

The image follows the standard `*_FILE` convention used by the
official Postgres / MySQL / Redis images: any env var ending in
`_FILE` whose value points to a readable file is consumed at
startup and exposed under the suffix-stripped name with the
file's text contents (trailing whitespace stripped). So
`GEMINI_API_KEY_FILE=/run/secrets/gemini_api_key` populates
`GEMINI_API_KEY` from the file, and the YAML's
`api_key: ${GEMINI_API_KEY}` interpolation finds it.

To set up:

```bash
mkdir -p secrets
printf '%s' '<your-gemini-key>'      > secrets/gemini_api_key.txt
printf '%s' '<your-telegram-token>'  > secrets/telegram_bot_token.txt
chmod 600 secrets/*
```

The `secrets/` directory is gitignored. If you'd rather use an
external secret store (Docker swarm secrets, Kubernetes Secrets, a
Vault sidecar, ...) the only requirement is that the secret lands
somewhere the container can read; the `*_FILE` pointer just needs
to be set in the env.

Operator-set canonical variables still win: if you `export
GEMINI_API_KEY=...` directly, the `_FILE` indirection is ignored
for that variable.

### UID/GID matching

The Dockerfile creates `taxonomaid` with `uid=1000` and `compose.yaml`
pins `user: "1000:1000"`. That matches the first non-root user on
most Linux distros (Synology DSM, Debian, Ubuntu desktop). It will
**not** match if your NAS already gave 1000 to a different account
(or if you want to write to a directory owned by a service account).
Two fixes:

- Easiest - change both the `useradd` line in `Dockerfile` and the
  `user:` line in `compose.yaml` to the actual `uid:gid` on the host,
  then `docker compose build`.
- Or `chown -R 1000:1000 /volume1/docs` (or whichever bind-mounted
  folder) so the in-container user can read+write.

If the daemon shows up healthy but every move fails with `EPERM` in
the logs, this is the cause.

### Config mount mode

`compose.yaml` mounts `/config:rw` because `taxonomaid mine` and
`taxonomaid review` write to `proposed_rules.yaml` / `rejected_rules.yaml`.
The dispatcher itself never writes to `/config`, so a determined operator
can mount that path read-only and run mine/review in a separate stack
against a writable copy - but that's beyond what the shipped compose
file does.

The compose file mounts `/volume1/docs` (a typical Synology path) into
the container. Adjust this to whichever directory you actually want
organised on your NAS, and either run `docker compose up -d` or use it
as a starting point inside an existing stack.

To run one-off operations:

```bash
docker compose run --rm taxonomaid doctor
docker compose run --rm taxonomaid health
docker compose run --rm taxonomaid mine
docker compose run --rm taxonomaid audit
docker compose run --rm taxonomaid review
```

### Log rotation

`data/decisions.jsonl` and `data/pending_decisions.jsonl` grow
monotonically; the miner replays the full audit log on every run, so
multi-year logs slow startup and the next `mine` invocation. The
dispatcher's in-memory similarity index is already capped at 50,000
tail samples, so warm-up stays fast, but the on-disk log isn't
rotated automatically.

A logrotate snippet sized for personal-NAS workloads ships at
[`deploy/logrotate.conf`](logrotate.conf). Edit the paths to match
your `data_dir`, then drop it in `/etc/logrotate.d/taxonomaid` (or
chain it from a user-level logrotate state file). The snippet uses
`copytruncate` because the dispatcher holds the file descriptors
under `flock`; rotating with `create` would break those handles.

Automatic rotation inside the daemon is a Phase-7 follow-up.
