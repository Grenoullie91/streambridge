#!/usr/bin/env bash
# ===================================================================
#  StreamBridge :: removal
#
#  Removes what install.sh created. Anything the user supplied is kept
#  unless --purge is given, so a mistaken uninstall cannot silently take
#  a favourites list with it.
#
#  Usage:
#    ./scripts/uninstall.sh --yes
#    ./scripts/uninstall.sh --yes --purge    # also delete config and data
# ===================================================================
set -euo pipefail

cd "$(dirname "$0")/.."

PREFIX="${HOME}/.local"
PURGE=0
ASSUME_YES=0
VENV_DIR=".venv"

usage() {
    sed -n '3,12p' "$0" | sed 's/^#  \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX="${2:?--prefix needs a directory}"; shift 2 ;;
        --venv)   VENV_DIR="${2:?--venv needs a directory}"; shift 2 ;;
        --purge)  PURGE=1; shift ;;
        -y|--yes) ASSUME_YES=1; shift ;;
        -h|--help) usage 0 ;;
        *)        printf 'Unknown option: %s\n' "$1" >&2; usage 1 ;;
    esac
done

say()  { printf '\033[1;32m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$1" >&2; }

# ------------------------------------------------------------------
#  Confirm
#
#  The service is stopped and files are deleted here, so ask first. This
#  is not a change to a running system the user can undo from the UI.
# ------------------------------------------------------------------
if [ "$ASSUME_YES" -eq 0 ]; then
    printf 'This stops the service and removes the installed files.\n'
    if [ "$PURGE" -eq 1 ]; then
        printf 'With --purge it also deletes your config, cache, favourites and history.\n'
    fi
    printf 'Continue? [y/N] '
    read -r reply
    case "$reply" in
        y|Y|yes|YES) ;;
        *) printf 'Aborted.\n'; exit 0 ;;
    esac
fi

BIN_DIR="${PREFIX}/bin"

# ------------------------------------------------------------------
#  Service
#
#  Disabled before the unit file is deleted: a unit that is enabled but
#  missing would otherwise be attempted at every login.
# ------------------------------------------------------------------
if command -v systemctl >/dev/null 2>&1 && [ -d "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user" ]; then
    if systemctl --user is-enabled streambridge.service >/dev/null 2>&1; then
        systemctl --user disable --now streambridge.service || warn "could not stop the service"
        say "stopped and disabled streambridge.service"
    fi
    rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/streambridge.service"
    systemctl --user daemon-reload || true
    say "removed the systemd user unit"
fi

# ------------------------------------------------------------------
#  Files
# ------------------------------------------------------------------
for name in streambridge streambridge-server; do
    if [ -e "${BIN_DIR}/${name}" ]; then
        rm -f "${BIN_DIR}/${name}"
        say "removed ${BIN_DIR}/${name}"
    fi
done

# Only a venv this script created is removed, and only when it holds no
# other project's package. A shared venv must survive.
if [ -d "$VENV_DIR" ]; then
    if "${VENV_DIR}/bin/python" -m pip list --format=freeze 2>/dev/null \
        | grep -qv '^streambridge'; then
        say "keeping ${VENV_DIR} (it holds other packages)"
    else
        rm -rf "$VENV_DIR"
        say "removed ${VENV_DIR}"
    fi
fi

# ------------------------------------------------------------------
#  User data, only with --purge
# ------------------------------------------------------------------
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config/streambridge}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state/streambridge}"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache/streambridge}"

if [ "$PURGE" -eq 1 ]; then
    rm -rf "$CONF_DIR" "$STATE_DIR" "$CACHE_DIR"
    say "removed config, favourites, history and cache"
else
    [ -d "$CONF_DIR" ]  && say "keeping ${CONF_DIR} (config)"
    [ -d "$STATE_DIR" ] && say "keeping ${STATE_DIR} (favourites and history)"
    [ -d "$CACHE_DIR" ] && say "keeping ${CACHE_DIR} (cache)"
    printf '\n  Re-run with --purge to delete those too.\n'
fi

# An MPD queue can still hold StreamBridge URLs. They stop resolving once
# the server is gone, so say so rather than leaving a silent failure.
warn "any tracks still queued in MPD point at the server that no longer runs"

printf '\n'
say "uninstalled."
