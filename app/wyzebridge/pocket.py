"""Pair a phone by scanning a QR code: the phone gets its own family login, kept in a cookie."""
import json
import secrets
import shutil
import socket
import subprocess
import time
from typing import Optional

import segno
from wyzebridge import config, guests
from wyzebridge.logging import logger

COOKIE = "wt_pocket"
PAIR_SECONDS = 600
# Browsers cap cookies at 400 days; the phone stays paired until it's removed on the Login page.
COOKIE_SECONDS = 400 * 86400
TAILSCALE_PATHS = "tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale", r"C:\Program Files\Tailscale\tailscale.exe"

_codes: dict[str, float] = {}


def new_code() -> str:
    now = time.time()
    for code in [c for c, t in _codes.items() if t < now]:
        del _codes[code]
    code = secrets.token_urlsafe(18)
    _codes[code] = now + PAIR_SECONDS
    return code


def claim(code: str, user_agent: str, owner: str) -> Optional[str]:
    """Use up a pairing code and return the phone's cookie, or None if the code is wrong or old."""
    if _codes.pop(code, 0) < time.time():
        return None
    kind = "iPhone" if "iPhone" in user_agent else "iPad" if "iPad" in user_agent else "Android" if "Android" in user_agent else "Phone"
    taken = {g["name"] for g in guests.list_guests()}
    name = next(n for i in range(1, 100) if (n := kind if i == 1 else f"{kind} {i}") not in taken)
    password = guests.add_guest(name, 0, owner)
    logger.info(f"[POCKET] Paired {name!r}")
    return f"{name}:{password}"


def from_cookie(value: str) -> tuple[str, str]:
    name, _, password = value.partition(":")
    return name, password


def lan_ip() -> str:
    """This computer's address on the home network, for a phone when the page was opened as localhost."""
    if config.BRIDGE_IP:
        return config.BRIDGE_IP
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))  # UDP connect sends nothing; it only picks the outgoing interface.
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def tailscale_host() -> str:
    """This computer's Tailscale name, so a phone on Tailscale can reach it from anywhere. Empty if not set up."""
    for path in TAILSCALE_PATHS:
        if not (exe := shutil.which(path)):
            continue
        try:
            out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=3).stdout
            status = json.loads(out)
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        if status.get("BackendState") == "Running":
            return status.get("Self", {}).get("DNSName", "").rstrip(".")
    return ""


def qr_svg(url: str) -> str:
    # Dark on white whatever the theme, so every phone camera can read it.
    return segno.make(url, error="m").svg_inline(scale=5, border=2, dark="#111", light="#fff")
