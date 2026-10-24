#!/usr/bin/env bash
# Remove Wyze Tunnel and its VM from this Mac:
#   curl -fsSL https://raw.githubusercontent.com/itskiwi/Wyze-Tunnel/main/mac/uninstall.sh | bash
set -euo pipefail
DIR="$HOME/Library/Application Support/Wyze Tunnel"
export LIMA_HOME="$DIR/vm"
PATH="$DIR/lima/bin:$PATH"
if command -v limactl >/dev/null; then
    limactl start-at-login --enabled=false wyze-tunnel </dev/null >/dev/null 2>&1 || true
    limactl delete -f wyze-tunnel </dev/null || true
fi
rm -rf "$DIR"
echo "Wyze Tunnel has been removed."
