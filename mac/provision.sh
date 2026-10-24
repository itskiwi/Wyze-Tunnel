#!/usr/bin/env bash
# Runs as root inside the Lima VM from a repo checkout: run the native installer and point WebRTC at the Mac's LAN IP.
# Usage: sudo mac/provision.sh <mac-lan-ip>
set -euo pipefail
SRC=$(cd "$(dirname "$0")/.." && pwd)
"$SRC/native/install.sh" >/dev/null
ENV=/etc/wyze-tunnel/wyze-tunnel.env
sed -i '/^WB_IP=/d' "$ENV" && echo "WB_IP=$1" >> "$ENV"
systemctl restart wyze-tunnel
sed -n 's/^WB_PASSWORD=//p' "$ENV"
