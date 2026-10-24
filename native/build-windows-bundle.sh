#!/usr/bin/env bash
# Build the bundle the Windows desktop app runs natively (no WSL): embeddable Python with the app's
# packages, the source, MediaMTX, go2rtc and ffmpeg. The Wyze SDK is Linux-only, so cameras connect through go2rtc.
# Usage: native/build-windows-bundle.sh [out-dir]   (set VERSION=x.y.z to stamp the version)
# Output: <out-dir>/wyze-tunnel-windows-x86_64.zip
set -euo pipefail

OUT=$(mkdir -p "${1:-dist}" && cd "${1:-dist}" && pwd)
SRC=$(cd "$(dirname "$0")/.." && pwd)
PY_VER=3.12.10

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
STAGE=$TMP/wyze-tunnel
mkdir -p "$STAGE/bin" "$STAGE/python"

git -C "$SRC" archive HEAD app | tar xf - -C "$STAGE"
rm -rf "$STAGE/app/lib"
[ -z "${VERSION:-}" ] || sed -i "s/^VERSION=.*/VERSION=$VERSION/" "$STAGE/app/.env"
MTX_TAG=$(grep '^MTX_TAG=' "$STAGE/app/.env" | cut -d= -f2)
GO2RTC_TAG=$(grep '^GO2RTC_TAG=' "$STAGE/app/.env" | cut -d= -f2)

get() { curl -fsSL -o "$TMP/dl.zip" "$1"; }

get "https://www.python.org/ftp/python/$PY_VER/python-$PY_VER-embed-amd64.zip"
unzip -q "$TMP/dl.zip" -d "$STAGE/python"
# The embeddable Python only searches what its ._pth lists.
printf 'python312.zip\r\n.\r\nLib\\site-packages\r\n..\\app\r\nimport site\r\n' > "$STAGE/python/python312._pth"
pip install --quiet --target "$STAGE/python/Lib/site-packages" --platform win_amd64 --python-version 3.12 \
    --implementation cp --only-binary=:all: -r "$STAGE/app/requirements.txt"

get "https://github.com/bluenviron/mediamtx/releases/download/v${MTX_TAG}/mediamtx_v${MTX_TAG}_windows_amd64.zip"
unzip -q "$TMP/dl.zip" mediamtx.exe mediamtx.yml -d "$STAGE"
get "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_TAG}/go2rtc_win64.zip"
unzip -q "$TMP/dl.zip" go2rtc.exe -d "$STAGE"
get "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
unzip -q -j "$TMP/dl.zip" '*/bin/ffmpeg.exe' '*/bin/ffprobe.exe' -d "$STAGE/bin"

(cd "$TMP" && zip -qr "$OUT/wyze-tunnel-windows-x86_64.zip" wyze-tunnel)
echo "$OUT/wyze-tunnel-windows-x86_64.zip"
