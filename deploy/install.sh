#!/usr/bin/env bash
# Install Taxonomaid as a user-level systemd service.
#
# Usage:
#   ./deploy/install.sh
#
# What it does:
#   1. Verifies a `taxonomaid` executable is on $PATH.
#   2. Creates ~/.config/taxonomaid and ~/.local/share/taxonomaid/data.
#   3. Copies the four example config files (if not already present).
#   4. Installs the systemd user units into ~/.config/systemd/user/.
#   5. Reloads the user daemon and prints next-step commands.
#
# Idempotent: re-running won't overwrite existing config or env file.

set -euo pipefail

if ! command -v taxonomaid >/dev/null 2>&1; then
    echo >&2 "error: 'taxonomaid' is not on PATH."
    echo >&2 "       install with: pipx install . (from the repo root)"
    echo >&2 "                or: uv tool install ."
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_DIR="${HOME}/.config/taxonomaid"
SECRETS_DIR="${CONFIG_DIR}/secrets"
DATA_DIR="${HOME}/.local/share/taxonomaid/data"
SYSTEMD_DIR="${HOME}/.config/systemd/user"

echo "==> creating directories"
mkdir -p "${CONFIG_DIR}" "${DATA_DIR}" "${SYSTEMD_DIR}"
# Tighten the secrets directory itself before any file lands inside.
mkdir -p "${SECRETS_DIR}"
chmod 700 "${SECRETS_DIR}"

echo "==> copying example configs (skip if already present)"
for name in watches llm notifier rules; do
    src="${REPO_ROOT}/config/${name}.example.yaml"
    dst="${CONFIG_DIR}/${name}.yaml"
    if [[ -f "${dst}" ]]; then
        echo "    keeping existing ${dst}"
    elif [[ -f "${src}" ]]; then
        cp "${src}" "${dst}"
        echo "    wrote ${dst}"
    fi
done

# Provision empty secret files so systemd's LoadCredential= directives
# resolve on first daemon start. The user fills these in once; the
# service unit then mounts them as $CREDENTIALS_DIRECTORY/<name> on a
# private tmpfs only the daemon can read.
echo "==> provisioning credential placeholders (chmod 600)"
for name in gemini_api_key telegram_bot_token; do
    dst="${SECRETS_DIR}/${name}"
    if [[ ! -e "${dst}" ]]; then
        : > "${dst}"
        echo "    wrote ${dst}"
    else
        echo "    keeping existing ${dst}"
    fi
    chmod 600 "${dst}"
done

echo "==> installing systemd user units"
for unit in taxonomaid.service taxonomaid-mine.service taxonomaid-mine.timer \
            taxonomaid-audit.service taxonomaid-audit.timer; do
    install -m 0644 "${REPO_ROOT}/deploy/systemd/${unit}" "${SYSTEMD_DIR}/${unit}"
    echo "    installed ${SYSTEMD_DIR}/${unit}"
done

echo "==> reloading systemd user daemon"
systemctl --user daemon-reload

cat <<EOF

Done. Next steps:

  1. Drop your secrets into the credential store
     (each file is a single-line value, no trailing newline needed):

       printf '%%s' '<your-gemini-key>'      > ${SECRETS_DIR}/gemini_api_key
       printf '%%s' '<your-telegram-token>'  > ${SECRETS_DIR}/telegram_bot_token

     They're already chmod 600 in a 700-mode directory. systemd
     mounts each into the service's private \$CREDENTIALS_DIRECTORY
     on a tmpfs - other processes running as the same user CANNOT
     read them via /proc/<pid>/environ the way EnvironmentFile=
     would have exposed them.

     For TPM-backed encryption-at-rest, see deploy/README.md
     (\`systemd-creds encrypt\` + \`LoadCredentialEncrypted=\`).

  2. Set TELEGRAM_CHAT_ID in a drop-in (the chat ID is not a
     credential, but it lives outside the YAML):

       systemctl --user edit taxonomaid.service
       # then add:
       #   [Service]
       #   Environment=TELEGRAM_CHAT_ID=987654321

  3. Edit ${HOME}/.config/taxonomaid/watches.yaml to point at your
     real folders.

  4. **Add your watched roots and destination_roots to ReadWritePaths=**
     via a systemd drop-in. The shipped unit declares
     ProtectSystem=strict + ProtectHome=read-only for safety; without
     an explicit grant the daemon cannot write into your folders.

     ReadWritePaths= is additive across drop-ins, so the cleanest
     pattern is to keep the shipped unit untouched and let the CLI
     generate the drop-in for you:

         taxonomaid systemd-paths --write-dropin \\
             -c ${HOME}/.config/taxonomaid \\
             -d ${HOME}/.local/share/taxonomaid/data

     This writes ${HOME}/.config/systemd/user/taxonomaid.service.d/readwritepaths.conf
     and survives future install.sh reruns. Re-run after editing
     watches.yaml to pick up new roots.

  5. Reload after the drop-in: systemctl --user daemon-reload
  6. Validate:                  taxonomaid doctor -c "${HOME}/.config/taxonomaid"
  7. Enable the daemon:         systemctl --user enable --now taxonomaid.service
  8. Optional weekly miner:     systemctl --user enable --now taxonomaid-mine.timer
  9. Optional weekly auditor:   systemctl --user enable --now taxonomaid-audit.timer
 10. Tail the logs:             journalctl --user -u taxonomaid.service -f
EOF
