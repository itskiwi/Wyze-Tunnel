#!/usr/bin/env bash
# Install or update Wyze Tunnel as a native systemd service (no Docker).
#
# One line, no git needed:
#   curl -fsSL https://raw.githubusercontent.com/itskiwi/Wyze-Tunnel/main/native/install.sh | sudo bash
# Or from a checkout:
#   sudo ./native/install.sh
# Re-run either one to update. Set WT_REF=<branch or tag> to install something other than main.
set -euo pipefail

# Everything runs inside main so a `curl | bash` pipe is fully read before anything executes.
main() {

PREFIX=/opt/wyze-tunnel
CONF_DIR=/etc/wyze-tunnel
REPO=itskiwi/Wyze-Tunnel

[ "$(id -u)" -eq 0 ] || { echo "Please run as root, e.g. with sudo." >&2; exit 1; }
command -v systemctl >/dev/null || { echo "This installer needs a Linux system with systemd." >&2; exit 1; }

has_deps() {
    command -v ffmpeg >/dev/null && command -v curl >/dev/null && command -v tar >/dev/null &&
        python3 -c 'import sys, ensurepip, venv; sys.exit(sys.version_info < (3, 10))' 2>/dev/null
}
if ! has_deps; then
    echo "==> Installing system packages (python3, ffmpeg, curl)"
    if command -v apt-get >/dev/null; then
        apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv ffmpeg curl tar </dev/null
    elif command -v dnf >/dev/null; then
        dnf install -y -q python3 ffmpeg-free curl tar
    elif command -v pacman >/dev/null; then
        pacman -Sy --noconfirm --needed python ffmpeg curl tar
    fi
    has_deps || { echo "Please install Python 3.10+ (with venv), ffmpeg and curl, then run this again." >&2; exit 1; }
fi

# Use the checkout this script sits in, or download the source when piped from curl.
SRC=$(cd "$(dirname "${BASH_SOURCE[0]:-.}")/.." 2>/dev/null && pwd)
if [ ! -f "$SRC/app/.env" ] || [ ! -f "$SRC/native/wyze-tunnel.service" ]; then
    SRC=$(mktemp -d)
    trap 'rm -rf "$SRC"' EXIT
    echo "==> Downloading Wyze Tunnel (${WT_REF:-main})"
    curl -fsSL "https://github.com/$REPO/archive/${WT_REF:-main}.tar.gz" | tar xzf - -C "$SRC" --strip-components=1
fi

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

IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}' || true)
IP=${IP:-$(hostname -I 2>/dev/null | awk '{print $1}' || true)}

mkdir -p "$CONF_DIR"
if [ ! -f "$CONF_DIR/wyze-tunnel.env" ]; then
    install -m 600 "$SRC/native/wyze-tunnel.env" "$CONF_DIR/wyze-tunnel.env"
    sed -i "s/^WB_IP=.*/WB_IP=$IP/" "$CONF_DIR/wyze-tunnel.env"
fi

install -m 644 "$SRC/native/wyze-tunnel.service" /etc/systemd/system/wyze-tunnel.service
systemctl daemon-reload
systemctl enable wyze-tunnel >/dev/null
systemctl restart wyze-tunnel

cat <<MSG

  Wyze Tunnel is installed and running.

  1. Open http://${IP:-<this-machine-ip>}:5000 in your browser.
  2. Sign in with your Wyze email, password, API ID and API key.
     Get the API ID and key at https://developer-api-console.wyze.com/#/apikey/view
  3. Next time, log in to the web page as "wbadmin" with the part of your
     Wyze email before the @ as the password.

  It starts automatically on boot. Logs: journalctl -u wyze-tunnel -f
  Advanced settings: $CONF_DIR/wyze-tunnel.env

MSG
}

main "$@"
