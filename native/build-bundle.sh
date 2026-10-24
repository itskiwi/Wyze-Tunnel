#!/usr/bin/env bash
# Build a ready-to-install bundle for one CPU: the source plus that CPU's Wyze library, MediaMTX, go2rtc and a static ffmpeg.
# Usage: native/build-bundle.sh <x86_64|aarch64|armv7l> [out-dir]   (set VERSION=x.y.z to stamp the version)
# Output: <out-dir>/wyze-tunnel-linux-<cpu>.tar.gz, named after `uname -m` so installers can pick theirs.
set -euo pipefail

ARCH=${1:?usage: $0 <x86_64|aarch64|armv7l> [out-dir]}
OUT=$(mkdir -p "${2:-dist}" && cd "${2:-dist}" && pwd)
SRC=$(cd "$(dirname "$0")/.." && pwd)

case "$ARCH" in
    x86_64) LIB_ARCH=amd64 MTX_ARCH=amd64 FFMPEG_ARCH=x86_64 G2R_ARCH=amd64 ;;
    aarch64) LIB_ARCH=arm64 MTX_ARCH=arm64v8 FFMPEG_ARCH=aarch64 G2R_ARCH=arm64 ;;
    armv7l) LIB_ARCH=arm MTX_ARCH=armv7 FFMPEG_ARCH=arm32v7 G2R_ARCH=arm ;;
    *) echo "Unknown CPU: $ARCH (use x86_64, aarch64 or armv7l)" >&2; exit 1 ;;
esac

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
STAGE=$TMP/wyze-tunnel
mkdir -p "$STAGE/bin"

git -C "$SRC" archive HEAD app native mac | tar xf - -C "$STAGE"
find "$STAGE/app/lib" -name 'lib.*' ! -name "lib.$LIB_ARCH" -delete
[ -z "${VERSION:-}" ] || sed -i "s/^VERSION=.*/VERSION=$VERSION/" "$STAGE/app/.env"
MTX_TAG=$(grep '^MTX_TAG=' "$STAGE/app/.env" | cut -d= -f2)
GO2RTC_TAG=$(grep '^GO2RTC_TAG=' "$STAGE/app/.env" | cut -d= -f2)

curl -fsSL "https://github.com/bluenviron/mediamtx/releases/download/v${MTX_TAG}/mediamtx_v${MTX_TAG}_linux_${MTX_ARCH}.tar.gz" \
    | tar xzf - -C "$STAGE" mediamtx mediamtx.yml
curl -fsSL -o "$STAGE/go2rtc" "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_TAG}/go2rtc_linux_${G2R_ARCH}"
chmod +x "$STAGE/go2rtc"
# Same static ffmpeg build the Docker image uses.
curl -fsSL "https://github.com/homebridge/ffmpeg-for-homebridge/releases/latest/download/ffmpeg-alpine-${FFMPEG_ARCH}.tar.gz" \
    | tar xzf - -C "$STAGE/bin" --strip-components=4 ./usr/local/bin/ffmpeg

tar czf "$OUT/wyze-tunnel-linux-$ARCH.tar.gz" -C "$TMP" wyze-tunnel
echo "$OUT/wyze-tunnel-linux-$ARCH.tar.gz"
