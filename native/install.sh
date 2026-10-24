#!/usr/bin/env bash
# Install or update Wyze Tunnel as a native systemd service (no Docker).
#
# One line, no git needed:
#   curl -fsSL https://raw.githubusercontent.com/itskiwi/Wyze-Tunnel/main/native/install.sh | sudo bash
# Or from a checkout:
#   sudo ./native/install.sh
# Re-run either one to update. The one-liner installs the latest release; set WT_REF=<branch or tag>
# to install that source instead.
set -euo pipefail

# Everything runs inside main so a `curl | bash` pipe is fully read before anything executes.
main() {

PREFIX=/opt/wyze-tunnel
CONF_DIR=/etc/wyze-tunnel
REPO=itskiwi/Wyze-Tunnel

[ "$(id -u)" -eq 0 ] || { echo "Please run as root, e.g. with sudo." >&2; exit 1; }
command -v systemctl >/dev/null || { echo "This installer needs a Linux system with systemd." >&2; exit 1; }

# --update is what the daily timer and the web UI's update button run: install the latest release only if it's newer.
UPDATE=
if [ "${1:-}" = --update ]; then
    rm -f /var/lib/private/wyze-tunnel/update-now
    LATEST=$(curl -fsSL -o /dev/null -w '%{url_effective}' "https://github.com/$REPO/releases/latest")
    LATEST=${LATEST##*/tag/v}
    CURRENT=$(sed -n 's/^VERSION=//p' "$PREFIX/app/.env" 2>/dev/null || true)
    if [[ ! $LATEST =~ ^[0-9]+\.[0-9]+\.[0-9]+ ]] || [ "$(printf '%s\n' "$LATEST" "$CURRENT" | sort -V | tail -1)" = "$CURRENT" ]; then
        echo "Wyze Tunnel is up to date (v$CURRENT)."
        return 0
    fi
    echo "==> Updating Wyze Tunnel v$CURRENT -> v$LATEST"
    UPDATE=1
    unset WT_REF
fi

# Use the checkout or bundle this script sits in. When piped from curl, download the latest
# release bundle for this CPU (or the source of WT_REF, or main if there is no release yet).
SRC=$(cd "$(dirname "${BASH_SOURCE[0]:-.}")/.." 2>/dev/null && pwd)
if [ -n "$UPDATE" ] || [ ! -f "$SRC/app/.env" ] || [ ! -f "$SRC/native/wyze-tunnel.service" ]; then
    SRC=$(mktemp -d)
    trap 'rm -rf "$SRC"' EXIT
    BUNDLE=wyze-tunnel-linux-$(uname -m).tar.gz
    RELEASE=https://github.com/$REPO/releases/latest/download
    if [ -z "${WT_REF:-}" ] && curl -fsSL "$RELEASE/$BUNDLE" -o "$SRC/$BUNDLE" 2>/dev/null; then
        echo "==> Downloaded the latest Wyze Tunnel release ($(uname -m))"
        curl -fsSL "$RELEASE/SHA256SUMS" -o "$SRC/SHA256SUMS"
        (cd "$SRC" && grep " $BUNDLE\$" SHA256SUMS | sha256sum -c --quiet) ||
            { echo "The download is corrupt (checksum mismatch). Please try again." >&2; exit 1; }
        tar xzf "$SRC/$BUNDLE" -C "$SRC" --strip-components=1
    else
        echo "==> Downloading Wyze Tunnel (${WT_REF:-main})"
        curl -fsSL "https://github.com/$REPO/archive/${WT_REF:-main}.tar.gz" | tar xzf - -C "$SRC" --strip-components=1
    fi
fi

# Release bundles ship their own ffmpeg, so only a source install needs the system one.
FFMPEG=ffmpeg
[ -x "$SRC/bin/ffmpeg" ] && FFMPEG=
has_deps() {
    { [ -z "$FFMPEG" ] || command -v ffmpeg >/dev/null; } && command -v curl >/dev/null && command -v tar >/dev/null &&
        python3 -c 'import sys, ensurepip, venv; sys.exit(sys.version_info < (3, 10))' 2>/dev/null
}
if ! has_deps; then
    echo "==> Installing system packages (python3, ${FFMPEG:+ffmpeg, }curl)"
    if command -v apt-get >/dev/null; then
        apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv $FFMPEG curl tar </dev/null
    elif command -v dnf >/dev/null; then
        dnf install -y -q python3 ${FFMPEG:+ffmpeg-free} curl tar
    elif command -v pacman >/dev/null; then
        pacman -Sy --noconfirm --needed python $FFMPEG curl tar
    fi
    has_deps || { echo "Please install Python 3.10+ (with venv), ${FFMPEG:+ffmpeg and }curl, then run this again." >&2; exit 1; }
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
# --remove-destination: the running service holds these open (cp would fail with "Text file busy").
cp --remove-destination "$PREFIX/app/lib/lib.$LIB_ARCH" "$PREFIX/libIOTCAPIs_ALL.so"
rm -rf "$PREFIX/app/lib"

echo "==> Installing Python dependencies"
[ -x "$PREFIX/venv/bin/python" ] || python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --disable-pip-version-check -r "$PREFIX/app/requirements.txt"
"$PREFIX/venv/bin/python" -m compileall -q "$PREFIX/app"

if [ -x "$SRC/mediamtx" ]; then
    cp --remove-destination "$SRC/mediamtx" "$SRC/mediamtx.yml" "$PREFIX/"
else
    echo "==> Downloading MediaMTX v$MTX_TAG ($MTX_ARCH)"
    curl -fsSL "https://github.com/bluenviron/mediamtx/releases/download/v${MTX_TAG}/mediamtx_v${MTX_TAG}_linux_${MTX_ARCH}.tar.gz" \
        | tar xzf - -C "$PREFIX" mediamtx mediamtx.yml
fi
rm -rf "$PREFIX/bin"
[ -z "$FFMPEG" ] && install -D -m 755 "$SRC/bin/ffmpeg" "$PREFIX/bin/ffmpeg"

IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}' || true)
IP=${IP:-$(hostname -I 2>/dev/null | awk '{print $1}' || true)}

mkdir -p "$CONF_DIR"
if [ ! -f "$CONF_DIR/wyze-tunnel.env" ]; then
    install -m 600 "$SRC/native/wyze-tunnel.env" "$CONF_DIR/wyze-tunnel.env"
    sed -i "s/^WB_IP=.*/WB_IP=$IP/" "$CONF_DIR/wyze-tunnel.env"
    sed -i "s/^#WB_PASSWORD=.*/WB_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')/" "$CONF_DIR/wyze-tunnel.env"
fi
WB_PASSWORD=$(sed -n 's/^WB_PASSWORD=//p' "$CONF_DIR/wyze-tunnel.env")

install -m 755 "$SRC/native/uninstall.sh" "$PREFIX/uninstall.sh"
install -m 755 "$SRC/native/install.sh" "$PREFIX/install.sh"
install -D -m 755 "$SRC/native/wyze-tunnel" /usr/local/bin/wyze-tunnel
import_docker

# Turn on daily updates for new installs only, so a `systemctl disable` sticks across re-runs.
NEW_TIMER=
[ -f /etc/systemd/system/wyze-tunnel-update.timer ] || NEW_TIMER=1
install -m 644 "$SRC"/native/wyze-tunnel.service "$SRC"/native/wyze-tunnel-update.{service,timer,path} /etc/systemd/system/
systemctl daemon-reload
systemctl enable wyze-tunnel >/dev/null
systemctl enable --now wyze-tunnel-update.path 2>/dev/null
[ -z "$NEW_TIMER" ] || systemctl enable --now wyze-tunnel-update.timer 2>/dev/null
systemctl restart wyze-tunnel

cat <<MSG

  Wyze Tunnel is installed and running.

  1. Open http://${IP:-<this-machine-ip>}:5000 in your browser.
  2. Sign in with your Wyze email, password, API ID and API key.
     Get the API ID and key at https://developer-api-console.wyze.com/#/apikey/view
  3. Then log in to the web page as "wbadmin" with the password:
     ${WB_PASSWORD:-the one you already use (set WB_PASSWORD in the settings file to change it)}

  It starts automatically on boot. Logs: journalctl -u wyze-tunnel -f
  Advanced settings: $CONF_DIR/wyze-tunnel.env
  Updates install automatically once a day (turn off: sudo systemctl disable --now wyze-tunnel-update.timer).
  Trouble? Run: wyze-tunnel help   (restart, logs, repair and more)
  Uninstall: sudo $PREFIX/uninstall.sh
MSG
if [ -n "$DOCKER_BRIDGE" ]; then
    cat <<MSG

  Moved over from your Docker container "$DOCKER_BRIDGE" (sign-in and snapshots).
  It uses the same ports, so stop it and keep it from coming back:
      docker stop $DOCKER_BRIDGE && docker update --restart=no $DOCKER_BRIDGE
  then: sudo systemctl restart wyze-tunnel
MSG
fi
echo
}

# Existing Docker users: copy the container's sign-in (/tokens) and snapshots (/img) into the
# service's state dir, once, so they don't have to sign in again.
import_docker() {
    DOCKER_BRIDGE=
    command -v docker >/dev/null || return 0
    local cid state=/var/lib/private/wyze-tunnel
    cid=$(docker ps -a --format '{{.ID}} {{.Image}}' 2>/dev/null | awk '$2 ~ /wyze-bridge|wyze-tunnel/ {print $1; exit}' || true)
    [ -n "$cid" ] || return 0
    DOCKER_BRIDGE=$(docker inspect -f '{{.Name}}' "$cid" | sed 's|^/||')
    [ -z "$(ls -A "$state/tokens" 2>/dev/null)" ] || return 0
    echo "==> Importing sign-in and snapshots from Docker container $DOCKER_BRIDGE"
    install -d -m 700 /var/lib/private
    mkdir -p "$state/tokens" "$state/img"
    docker cp "$cid:/tokens/." "$state/tokens/" 2>/dev/null || true
    docker cp "$cid:/img/." "$state/img/" 2>/dev/null || true
    # Match the state dir's owner (systemd's dynamic user after a first run, root before).
    chown -R --reference="$state" "$state"
}

main "$@"
