#!/usr/bin/env bash
# Uninstall the Taxonomaid systemd user service and (optionally) state.
#
# Usage:
#   ./deploy/uninstall.sh           # remove units; keep config + data + secrets
#   ./deploy/uninstall.sh --purge   # also remove ~/.config/taxonomaid
#                                   # AND ~/.local/share/taxonomaid
#
# Idempotent: re-running won't error if a unit is already disabled or
# a directory is already gone. Refuses to run when --purge would
# delete a path that's being kept (e.g. you've moved data elsewhere
# and want only the units removed); use the no-arg form in that case.

set -euo pipefail

PURGE=0
for arg in "$@"; do
    case "${arg}" in
        --purge) PURGE=1 ;;
        -h|--help)
            sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo >&2 "error: unknown argument: ${arg}"
            echo >&2 "usage: $(basename "$0") [--purge|-h]"
            exit 2
            ;;
    esac
done

CONFIG_DIR="${HOME}/.config/taxonomaid"
DATA_DIR="${HOME}/.local/share/taxonomaid"
SYSTEMD_DIR="${HOME}/.config/systemd/user"
DROPIN_DIR="${SYSTEMD_DIR}/taxonomaid.service.d"

UNITS=(
    taxonomaid.service
    taxonomaid-mine.service
    taxonomaid-mine.timer
    taxonomaid-audit.service
    taxonomaid-audit.timer
)

echo "==> stopping and disabling units"
for unit in "${UNITS[@]}"; do
    # `disable --now` stops if running, disables if enabled, no-op if
    # neither. The leading `|| true` guards against units that aren't
    # installed (a clean install.sh→uninstall.sh sequence on a stock
    # systemd will print warnings, not errors, but we suppress to
    # keep the output tidy).
    if systemctl --user list-unit-files --no-legend --type=service,timer 2>/dev/null \
       | awk '{print $1}' | grep -qx "${unit}"; then
        systemctl --user disable --now "${unit}" 2>/dev/null || true
        echo "    disabled ${unit}"
    fi
done

echo "==> removing unit files"
for unit in "${UNITS[@]}"; do
    target="${SYSTEMD_DIR}/${unit}"
    if [[ -e "${target}" ]]; then
        rm -f "${target}"
        echo "    removed ${target}"
    fi
done

if [[ -d "${DROPIN_DIR}" ]]; then
    echo "==> removing drop-in directory ${DROPIN_DIR}"
    rm -rf "${DROPIN_DIR}"
fi

echo "==> reloading systemd user daemon"
systemctl --user daemon-reload

if (( PURGE )); then
    echo "==> --purge: removing config + data + secrets"
    if [[ -d "${CONFIG_DIR}" ]]; then
        # Sanity guard: refuse to recursively delete a symlink target.
        if [[ -L "${CONFIG_DIR}" ]]; then
            echo >&2 "    refusing to follow symlink at ${CONFIG_DIR}; remove it manually"
        else
            rm -rf "${CONFIG_DIR}"
            echo "    removed ${CONFIG_DIR}"
        fi
    fi
    if [[ -d "${DATA_DIR}" ]]; then
        if [[ -L "${DATA_DIR}" ]]; then
            echo >&2 "    refusing to follow symlink at ${DATA_DIR}; remove it manually"
        else
            rm -rf "${DATA_DIR}"
            echo "    removed ${DATA_DIR}"
        fi
    fi
fi

cat <<EOF

Done. Notes:

  - The 'taxonomaid' executable itself is left in place; remove with
    'pipx uninstall taxonomaid' or 'uv tool uninstall taxonomaid'.
EOF

if (( PURGE == 0 )); then
    cat <<EOF
  - Your config, secrets, and runtime state survive at:
        ${CONFIG_DIR}
        ${DATA_DIR}
    Re-run with --purge to remove them, or 'rm -rf' them by hand.
EOF
fi
