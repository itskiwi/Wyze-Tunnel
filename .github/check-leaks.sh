#!/usr/bin/env bash
# Fail when tracked files hold something personal or secret: a real email, home network address,
# camera MAC, API key or private key, or an image outside the folders meant for icons and screenshots.
# Made-up values the docs and tests use on purpose are listed in ALLOW. Run from the repo root.
set -euo pipefail

ALLOW='192\.168\.[0-3]\.[0-9]+|192\.168\.50\.27|10\.0\.0\.21|10\.255\.255\.255|172\.30\.32\.2|2C:AA:8E:01:02:03'
ALLOW+='|@(example\.com|mail\.com|doe\.com|bridge\.local|users\.noreply\.github\.com)|noreply@anthropic\.com|myEmail123@'
ALLOW+='|@2x\.png|tail1234\.ts\.net|wt_your_key|whsec_\.\.\.|100\.64\.0\.0|:10\.0\.0\.0$|172\.16\.0\.0|100\.101\.102\.103|100\.64\.1\.2'

PATTERNS=(
    '\b(192\.168|10\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01]))\.[0-9]{1,3}\.[0-9]{1,3}\b'   # home network addresses
    '\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}\b|[a-z0-9-]+\.ts\.net\b'   # Tailscale
    '\b[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}\b'                     # MAC addresses
    '[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]*[A-Za-z]{2,}'  # emails
    '\b(wt_|whsec_|ghp_|github_pat_|xox[baprs]-|sk-|AKIA)[A-Za-z0-9_-]{8,}'  # API keys and tokens
    '-----BEGIN [A-Z ]*PRIVATE KEY'
)

status=0
files=$(git ls-files | grep -vE '\.(png|jpg|jpeg|gif|ico|icns|webp|svg|woff2?|whl|lock)$|^app/lib/|^app/static/tailwind\.css$')
for pattern in "${PATTERNS[@]}"; do
    # shellcheck disable=SC2086
    hits=$(grep -nEo "$pattern" $files 2>/dev/null | grep -vE "$ALLOW" || true)
    if [ -n "$hits" ]; then
        echo "Looks personal or secret (add made-up values to ALLOW in $0):"
        echo "$hits"
        status=1
    fi
done

# Pictures only where icons and screenshots live, so a camera snapshot can't slip in with a commit.
bad_images=$(git ls-files | grep -iE '\.(png|jpg|jpeg|gif|webp|bmp|mp4|mkv|mov)$' |
    grep -vE '^(docs/img|app/static|desktop/src-tauri/icons|home_assistant(/[a-z]+)?)/[^/]+$' || true)
if [ -n "$bad_images" ]; then
    echo "Images and videos belong in docs/img (screenshots with made-up data) or the icon folders:"
    echo "$bad_images"
    status=1
fi

[ "$status" -eq 0 ] && echo "No personal data or secrets found in tracked files."
exit "$status"
