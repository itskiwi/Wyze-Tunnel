#!/usr/bin/env bash
# Install or update Wyze Tunnel on a Mac. Paste into Terminal:
#   curl -fsSL https://raw.githubusercontent.com/itskiwi/Wyze-Tunnel/main/mac/install.sh | bash
# The Wyze camera library only exists for Linux, so this runs the native Linux
# install inside a small background VM (Lima, no Docker or Homebrew needed).
set -euo pipefail

VM=wyze-tunnel
REF=${WT_REF:-main}
DIR="$HOME/Library/Application Support/Wyze Tunnel"
LIMA_HOME="$DIR/vm"
export LIMA_HOME
PATH="$DIR/lima/bin:$PATH"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

main() {
    [ "$(uname -s)" = Darwin ] || { echo "This installer is for macOS." >&2; exit 1; }
    case "$(uname -m)" in
        arm64) ARCH=arm64 ;;
        x86_64) ARCH=x86_64 ;;
        *) echo "Unsupported Mac CPU: $(uname -m)" >&2; exit 1 ;;
    esac
    mkdir -p "$DIR"

    if ! command -v limactl >/dev/null; then
        say "Downloading the VM tool (Lima)"
        TAG=$(curl -fsSLI -o /dev/null -w '%{url_effective}' https://github.com/lima-vm/lima/releases/latest)
        TAG=${TAG##*/}
        mkdir -p "$DIR/lima"
        curl -fsSL "https://github.com/lima-vm/lima/releases/download/$TAG/lima-${TAG#v}-Darwin-$ARCH.tar.gz" \
            | tar xzf - -C "$DIR/lima"
    fi

    if ! limactl list -q 2>/dev/null | grep -qx "$VM"; then
        say "Creating the Wyze Tunnel VM (one time, takes a few minutes)"
        # Web UI goes to Mac port 5050 because macOS AirPlay already uses 5000.
        limactl create --name="$VM" --tty=false \
            --set '.cpus=2 | .memory="2GiB" | .disk="10GiB" | .mounts=[]' \
            --set '.portForwards=[
                {"guestPort":5000,"hostPort":5050,"hostIP":"0.0.0.0"},
                {"guestPort":8554,"hostIP":"0.0.0.0"},
                {"guestPort":8888,"hostIP":"0.0.0.0"},
                {"guestPort":8889,"hostIP":"0.0.0.0"},
                {"guestPort":1935,"hostIP":"0.0.0.0"},
                {"guestPort":8189,"hostIP":"0.0.0.0","proto":"udp"}]' \
            template://debian-12 </dev/null
    fi

    say "Starting the VM"
    limactl start --tty=false "$VM" </dev/null
    limactl start-at-login "$VM" </dev/null >/dev/null 2>&1 || true

    # shortcut: WB_IP is the Mac's current Wi-Fi/Ethernet IP; re-run this installer if it changes.
    MAC_IP=$(ipconfig getifaddr en0 || ipconfig getifaddr en1 || echo 127.0.0.1)

    say "Installing Wyze Tunnel inside the VM"
    limactl shell --workdir / "$VM" sudo bash -euo pipefail -c "
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq && apt-get install -y -qq python3-venv ffmpeg curl >/dev/null
        rm -rf /tmp/wt && mkdir /tmp/wt
        curl -fsSL https://github.com/itskiwi/Wyze-Tunnel/archive/$REF.tar.gz | tar xzf - -C /tmp/wt --strip-components=1
        /tmp/wt/native/install.sh >/dev/null
        ENV=/etc/wyze-tunnel/wyze-tunnel.env
        sed -i '/^WB_IP=/d' \$ENV && echo WB_IP=$MAC_IP >> \$ENV
        systemctl restart wyze-tunnel
    " </dev/null

    say "Done! Wyze Tunnel is running."
    echo "  Open:  http://localhost:5050  (from other devices: http://$MAC_IP:5050)"
    echo "  RTSP:  rtsp://$MAC_IP:8554/<camera-name>"
    echo "  It starts automatically when you log in. Run this command again any time to update."
    sleep 3 && open http://localhost:5050 || true
}

main "$@"
