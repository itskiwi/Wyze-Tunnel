#!/usr/bin/env bash
# Build a Tunnel Box disk image: a ready-to-flash OS with Wyze Tunnel already installed. Plugged in, the box
# answers at http://wyze-tunnel.local and shows a setup QR code on a screen, if one is attached.
# Usage: sudo box/build-image.sh <pi|pc> <wyze-tunnel-linux-CPU.tar.gz> [out-dir]
#   pi: Raspberry Pi 4/5 (Raspberry Pi OS Lite). Build on an arm64 machine.
#   pc: 64-bit Intel/AMD mini PC like an N100 (Debian). Build on an x86_64 machine.
# Needs losetup, growpart (cloud-guest-utils), resize2fs and xz. Output: <out-dir>/wyze-tunnel-box-<board>.img.xz
set -euo pipefail

BOARD=${1:?usage: $0 <pi|pc> <bundle.tar.gz> [out-dir]}
BUNDLE=$(realpath "${2:?usage: $0 <pi|pc> <bundle.tar.gz> [out-dir]}")
OUT=$(mkdir -p "${3:-dist}" && cd "${3:-dist}" && pwd)
HERE=$(cd "$(dirname "$0")" && pwd)

case "$BOARD" in
    pi) CPU=aarch64 ROOT_PART=2 BOOT_PART=1 BOOT_DIR=boot/firmware
        BASE=https://downloads.raspberrypi.com/raspios_lite_arm64_latest ;;
    pc) CPU=x86_64 ROOT_PART=1 BOOT_PART=15 BOOT_DIR=boot/efi
        BASE=https://cloud.debian.org/images/cloud/trixie/latest/debian-13-nocloud-amd64.tar.xz ;;
    *) echo "Unknown board: $BOARD (use pi or pc)" >&2; exit 1 ;;
esac
# Software is installed by running it in the image, so the build machine needs the same CPU.
[ "$(uname -m)" = "$CPU" ] || { echo "Build the $BOARD image on an $CPU machine." >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "Please run as root, e.g. with sudo." >&2; exit 1; }

WORK=$(mktemp -d)
IMG=$WORK/wyze-tunnel-box-$BOARD.img
MNT=$WORK/root
LOOP=
cleanup() {
    umount -R "$MNT" 2>/dev/null || true
    [ -z "$LOOP" ] || losetup -d "$LOOP"
    rm -rf "$WORK"
}
trap cleanup EXIT

echo "==> Downloading the base OS"
if [ "$BOARD" = pi ]; then
    curl -fsSL "$BASE" | xz -d > "$IMG"
else
    curl -fsSL "$BASE" | tar xJf - -C "$WORK" && mv "$WORK/disk.raw" "$IMG"
fi

echo "==> Making room for Wyze Tunnel"
truncate -s +1536M "$IMG"
growpart "$IMG" "$ROOT_PART"
LOOP=$(losetup -fP --show "$IMG")
e2fsck -fy "${LOOP}p$ROOT_PART" >/dev/null || [ $? -le 1 ]
resize2fs "${LOOP}p$ROOT_PART"

mkdir -p "$MNT"
mount "${LOOP}p$ROOT_PART" "$MNT"
mount "${LOOP}p$BOOT_PART" "$MNT/$BOOT_DIR"
for d in dev dev/pts proc sys; do mount --bind "/$d" "$MNT/$d"; done
# The image's resolv.conf may point at a resolver that isn't running here; put it back afterwards.
mv "$MNT/etc/resolv.conf" "$WORK/resolv.conf"
cp -L /etc/resolv.conf "$MNT/etc/resolv.conf"
# Don't let package installs start services inside the build.
printf '#!/bin/sh\nexit 101\n' > "$MNT/usr/sbin/policy-rc.d"
chmod +x "$MNT/usr/sbin/policy-rc.d"

mkdir -p "$MNT/tmp/wt"
tar xzf "$BUNDLE" -C "$MNT/tmp/wt" --strip-components=1
install -m 755 "$HERE/wyze-tunnel-box" "$MNT/usr/local/sbin/wyze-tunnel-box"
install -m 644 "$HERE/wyze-tunnel-box.service" "$MNT/etc/systemd/system/"
install -m 644 "$HERE/wyze-tunnel-box.conf" -D "$MNT/etc/systemd/system/wyze-tunnel.service.d/box.conf"

echo "==> Installing Wyze Tunnel into the image"
chroot "$MNT" /usr/bin/env BOARD="$BOARD" bash -euxo pipefail <<'CHROOT'
export DEBIAN_FRONTEND=noninteractive
# Mini PC network cards often need Debian's firmware packages.
[ "$BOARD" = pi ] || sed -i '/^Components:/{/non-free-firmware/!s/$/ non-free-firmware/}' /etc/apt/sources.list.d/debian.sources
apt-get update -qq
# avahi answers to wyze-tunnel.local; qrencode draws the setup QR code; growpart fills a bigger disk (pc).
apt-get install -y -qq --no-install-recommends avahi-daemon qrencode python3 python3-venv curl ca-certificates \
    $([ "$BOARD" = pc ] && echo cloud-guest-utils firmware-realtek firmware-misc-nonfree)
WT_IMAGE=1 /tmp/wt/native/install.sh
# Serve the web page on the plain port, so the address is just http://wyze-tunnel.local.
echo FLASK_RUN_PORT=80 >> /etc/wyze-tunnel/wyze-tunnel.env
systemctl enable wyze-tunnel-box avahi-daemon

echo wyze-tunnel > /etc/hostname
sed -i '/^127\.0\.1\.1/d' /etc/hosts
echo '127.0.1.1 wyze-tunnel' >> /etc/hosts
if [ "$BOARD" = pi ]; then
    # Skip the "create a user" prompt on first boot; Raspberry Pi Imager's settings (Wi-Fi, SSH, user) still apply.
    systemctl disable userconfig 2>/dev/null || true
else
    # The base image lets anyone log in as root without a password.
    passwd -l root
fi

apt-get clean
rm -rf /tmp/wt /var/lib/apt/lists/*
# Each box makes its own machine id on first boot.
: > /etc/machine-id
CHROOT

rm -f "$MNT/usr/sbin/policy-rc.d" "$MNT/etc/resolv.conf"
mv "$WORK/resolv.conf" "$MNT/etc/resolv.conf"
umount -R "$MNT"
losetup -d "$LOOP"
LOOP=

echo "==> Compressing"
xz -T0 -6 -c "$IMG" > "$OUT/wyze-tunnel-box-$BOARD.img.xz"
echo "$OUT/wyze-tunnel-box-$BOARD.img.xz"
