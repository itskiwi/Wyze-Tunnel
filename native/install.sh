#!/usr/bin/env bash
# Install or update Wyze Tunnel as a native systemd service (no Docker).
# Usage: sudo ./native/install.sh
set -euo pipefail

PREFIX=/opt/wyze-tunnel
CONF_DIR=/etc/wyze-tunnel
SRC=$(cd "$(dirname "$0")/.." && pwd)

[ "$(id -u)" -eq 0 ] || { echo "Please run as root: sudo $0" >&2; exit 1; }

for cmd in python3 ffmpeg curl tar systemctl; do
    command -v "$cmd" >/dev/null || {
        echo "Missing '$cmd'. On Debian/Ubuntu/Raspberry Pi OS: sudo apt install python3-venv ffmpeg curl" >&2
        exit 1
    }
done
python3 -c 'import sys, venv; sys.exit(sys.version_info < (3, 10))' 2>/dev/null || {
    echo "Python 3.10+ with venv is required (sudo apt install python3-venv)" >&2
    exit 1
}

case "$(uname -m)" in
    x86_64) LIB_ARCH=amd64 MTX_ARCH=amd64 ;;
    aarch64) LIB_ARCH=arm64 MTX_ARCH=arm64v8 ;;
    armv7l) LIB_ARCH=arm MTX_ARCH=armv7 ;;
    *) echo "Unsupported CPU: $(uname -m) (need x86_64, aarch64 or armv7l)" >&2; exit 1 ;;
esac
MTX_TAG=$(grep '^MTX_TAG=' "$SRC/app/.env" | cut -d= -f2)

echo "==> Installing app to $PREFIX"
mkdir -p "$PREFIX"
rm -rf "$PREFIX/app"
cp -r "$SRC/app" "$PREFIX/app"
cp "$PREFIX/app/lib/lib.$LIB_ARCH" "$PREFIX/libIOTCAPIs_ALL.so"
rm -rf "$PREFIX/app/lib"

echo "==> Installing Python dependencies"
[ -x "$PREFIX/venv/bin/python" ] || python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --disable-pip-version-check -r "$PREFIX/app/requirements.txt"
"$PREFIX/venv/bin/python" -m compileall -q "$PREFIX/app"

echo "==> Downloading MediaMTX v$MTX_TAG ($MTX_ARCH)"
curl -fsSL "https://github.com/bluenviron/mediamtx/releases/download/v${MTX_TAG}/mediamtx_v${MTX_TAG}_linux_${MTX_ARCH}.tar.gz" \
    | tar xzf - -C "$PREFIX" mediamtx mediamtx.yml

mkdir -p "$CONF_DIR"
if [ ! -f "$CONF_DIR/wyze-tunnel.env" ]; then
    install -m 600 "$SRC/native/wyze-tunnel.env" "$CONF_DIR/wyze-tunnel.env"
    echo "==> Created $CONF_DIR/wyze-tunnel.env"
fi

install -m 644 "$SRC/native/wyze-tunnel.service" /etc/systemd/system/wyze-tunnel.service
systemctl daemon-reload
systemctl enable wyze-tunnel >/dev/null
systemctl restart wyze-tunnel

echo
echo "Wyze Tunnel is running: http://$(hostname -I 2>/dev/null | awk '{print $1}'):5000"
echo "Settings: $CONF_DIR/wyze-tunnel.env   Logs: journalctl -u wyze-tunnel -f"
