#!/usr/bin/env bash
# Install or update Wyze Tunnel on a Mac. Paste into Terminal:
#   curl -fsSL https://raw.githubusercontent.com/itskiwi/Wyze-Tunnel/main/mac/install.sh | bash
# The Wyze camera library only exists for Linux, so this runs the native Linux
# install inside a small background VM (Lima, no Docker or Homebrew needed).
set -euo pipefail

VM=wyze-tunnel
REF=${WT_REF:-main}
DIR="$HOME/Library/Application Support/Wyze Tunnel"
# Lima keeps sockets in here; macOS caps socket paths at 104 chars, so it must stay short.
LIMA_HOME="$HOME/.wyze-tunnel"
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

    if [ ! -x "$DIR/lima/bin/limactl" ]; then
        say "Downloading the VM tool (Lima)"
        TAG=$(curl -fsSLI -o /dev/null -w '%{url_effective}' https://github.com/lima-vm/lima/releases/latest)
        TAG=${TAG##*/}
        mkdir -p "$DIR/lima"
        curl -fsSL "https://github.com/lima-vm/lima/releases/download/$TAG/lima-${TAG#v}-Darwin-$ARCH.tar.gz" \
            | tar xzf - -C "$DIR/lima"
    fi

    # shortcut: VMs from the first release lived in "$DIR/vm" and are recreated (sign in again); fine while installs are new.
    if [ -d "$DIR/vm" ]; then
        LIMA_HOME="$DIR/vm" limactl delete -f "$VM" </dev/null >/dev/null 2>&1 || true
        rm -rf "$DIR/vm"
    fi

    if ! limactl list -q 2>/dev/null | grep -qx "$VM"; then
        say "Creating the Wyze Tunnel VM (one time, takes a few minutes)"
        curl -fsSL "https://raw.githubusercontent.com/itskiwi/Wyze-Tunnel/$REF/mac/wyze-tunnel.yaml" -o "$DIR/wyze-tunnel.yaml"
        limactl create --name="$VM" --tty=false "$DIR/wyze-tunnel.yaml" </dev/null
    fi

    # `wyze-tunnel` rescue command (restart, logs, repair...) for new Terminal windows.
    mkdir -p "$DIR/bin"
    curl -fsSL "https://raw.githubusercontent.com/itskiwi/Wyze-Tunnel/$REF/mac/wyze-tunnel" -o "$DIR/bin/wyze-tunnel"
    chmod +x "$DIR/bin/wyze-tunnel"
    # .zshrc, not .zprofile: VS Code's and other non-login terminals skip .zprofile.
    grep -qs 'Wyze Tunnel/bin' "$HOME/.zshrc" ||
        echo 'export PATH="$HOME/Library/Application Support/Wyze Tunnel/bin:$PATH"' >> "$HOME/.zshrc"

    say "Starting the VM"
    limactl start --tty=false "$VM" </dev/null
    limactl start-at-login "$VM" </dev/null >/dev/null 2>&1 || true

    # shortcut: WB_IP is the Mac's current Wi-Fi/Ethernet IP; re-run this installer if it changes.
    MAC_IP=$(ipconfig getifaddr en0 || ipconfig getifaddr en1 || echo 127.0.0.1)

    say "Installing Wyze Tunnel inside the VM"
    # The latest release bundle for the VM's CPU, or the source of WT_REF.
    # provision.sh prints only the web password, so stdout is captured.
    WB_PASSWORD=$(limactl shell --workdir / "$VM" sudo bash -euo pipefail -s "$MAC_IP" "${WT_REF:-}" <<'VM'
    # Wrapped in a function so bash reads the whole script before anything can touch stdin.
    vm() {
        command -v curl >/dev/null || { apt-get update -qq && apt-get install -y -qq curl >/dev/null; }
        rm -rf /tmp/wt && mkdir /tmp/wt && cd /tmp/wt
        BUNDLE=wyze-tunnel-linux-$(uname -m).tar.gz
        RELEASE=https://github.com/itskiwi/Wyze-Tunnel/releases/latest/download
        if [ -z "$2" ] && curl -fsSL "$RELEASE/$BUNDLE" -o "$BUNDLE" 2>/dev/null; then
            curl -fsSL "$RELEASE/SHA256SUMS" | grep " $BUNDLE\$" | sha256sum -c --quiet
            tar xzf "$BUNDLE" --strip-components=1
        else
            curl -fsSL "https://github.com/itskiwi/Wyze-Tunnel/archive/${2:-main}.tar.gz" | tar xzf - --strip-components=1
        fi
        /tmp/wt/mac/provision.sh "$1"
    }
    vm "$@" </dev/null
VM
)

    say "Done! Wyze Tunnel is running."
    echo "  Open:  http://localhost:5050  (from other devices: http://$MAC_IP:5050)"
    echo "  Sign in with your Wyze email, password, API ID and API key"
    echo "  (get the API ID and key at https://developer-api-console.wyze.com/#/apikey/view)."
    if [ -n "$WB_PASSWORD" ]; then
        echo "  Then log in with username \"wbadmin\" and password: $WB_PASSWORD (change it on the Login page)"
    else
        echo "  Then log in with the username and password you already use."
    fi
    echo "  RTSP:  rtsp://$MAC_IP:8554/<camera-name>"
    echo "  It starts automatically when you log in and updates itself daily."
    echo "  Trouble? Open a new Terminal window and run: wyze-tunnel help"
    [ -n "${WT_NO_OPEN:-}" ] || { sleep 3 && open http://localhost:5050 || true; }
}

main "$@"
