#!/usr/bin/env bash
# Remove the native Wyze Tunnel install. Settings and sign-in data are kept unless you pass --purge.
#   sudo /opt/wyze-tunnel/uninstall.sh [--purge]
set -euo pipefail

main() {
    [ "$(id -u)" -eq 0 ] || { echo "Please run as root, e.g. with sudo." >&2; exit 1; }
    systemctl disable --now wyze-tunnel 2>/dev/null || true
    rm -f /etc/systemd/system/wyze-tunnel.service
    systemctl daemon-reload
    rm -rf /opt/wyze-tunnel
    if [ "${1:-}" = "--purge" ]; then
        rm -rf /etc/wyze-tunnel /var/lib/private/wyze-tunnel /var/lib/wyze-tunnel
        echo "Wyze Tunnel and all of its data were removed."
    else
        echo "Wyze Tunnel was removed."
        echo "Kept your settings (/etc/wyze-tunnel) and sign-in data (/var/lib/private/wyze-tunnel)."
        echo "Run again with --purge to delete those too."
    fi
}

main "$@"
