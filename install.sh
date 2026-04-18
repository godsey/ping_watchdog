#!/usr/bin/env bash
#
# Copyright (c) 2026 Jason Godsey <jason@godsey.net>
# Licensed under the MIT License. See LICENSE.txt for details.

set -eu

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

SCRIPT_SRC="$REPO_DIR/usr/local/bin/ping_watchdog.py"
DEFAULTS_SRC="$REPO_DIR/etc/default/ping_watchdog"
SERVICE_SRC="$REPO_DIR/etc/systemd/system/ping_watchdog.service"
TIMER_SRC="$REPO_DIR/etc/systemd/system/ping_watchdog.timer"

SCRIPT_DST="/usr/local/bin/ping_watchdog.py"
DEFAULTS_DST="/etc/default/ping_watchdog"
SERVICE_DST="/etc/systemd/system/ping_watchdog.service"
TIMER_DST="/etc/systemd/system/ping_watchdog.timer"

FORCE=false

usage() {
    cat <<EOF
Usage: $0 [--force] [--help]

Options:
  --force   Install even if this host does not appear to be Proxmox
  --help    Show this help
EOF
}

is_proxmox() {
    [ -x /usr/sbin/qm ] && return 0
    [ -d /etc/pve ] && return 0
    command -v pveversion >/dev/null 2>&1 && return 0
    return 1
}

check_dependencies() {
    if ! command -v python3 >/dev/null 2>&1; then
        echo "Error: python3 is not installed."
        exit 1
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --force)
            FORCE=true
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done

if [ "$(id -u)" != "0" ]; then
    echo "Please run as root."
    exit 1
fi

check_dependencies

if ! is_proxmox; then
    if [ "$FORCE" != "true" ]; then
        echo "This host does not appear to be Proxmox VE."
        echo "Use --force to install anyway."
        exit 1
    fi

    echo "Warning: Proxmox detection failed, continuing because --force was given."
fi

for f in "$SCRIPT_SRC" "$DEFAULTS_SRC" "$SERVICE_SRC" "$TIMER_SRC"; do
    if [ ! -f "$f" ]; then
        echo "Missing file: $f"
        exit 1
    fi
done

echo "Installing ping_watchdog.py to $SCRIPT_DST"
install -m 0755 "$SCRIPT_SRC" "$SCRIPT_DST"

echo "Installing /etc/default/ping_watchdog"
install -m 0644 "$DEFAULTS_SRC" "$DEFAULTS_DST"

echo "Installing ping_watchdog.service"
install -m 0644 "$SERVICE_SRC" "$SERVICE_DST"

echo "Installing ping_watchdog.timer"
install -m 0644 "$TIMER_SRC" "$TIMER_DST"

echo "Reloading systemd"
systemctl daemon-reload

echo "Enabling and starting timer"
systemctl enable --now ping_watchdog.timer

echo
echo "Timer status:"
systemctl status ping_watchdog.timer --no-pager || true

echo
echo "Recent service log:"
journalctl -u ping_watchdog.service -n 10 --no-pager || true

echo
echo "Done."

