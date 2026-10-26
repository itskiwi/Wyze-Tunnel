"""Pair a phone by scanning a QR code: the phone gets its own family login, kept in a cookie."""
import json
import os
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path
from subprocess import DEVNULL, PIPE, Popen
from typing import Optional

import segno
from flask import request
from wyzebridge import config, guests, webhooks
from wyzebridge.go2rtc import GO2RTC_BIN
from wyzebridge.logging import logger

COOKIE = "wt_pocket"
PAIR_SECONDS = 600
# Browsers cap cookies at 400 days; the phone stays paired until it's removed on the Login page.
COOKIE_SECONDS = 400 * 86400
TAILSCALE_PATHS = "tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale", r"C:\Program Files\Tailscale\tailscale.exe"

# native/wyze-away: embedded Tailscale, shipped next to go2rtc in the Windows bundle.
AWAY_BIN = os.getenv("WYZE_AWAY_BIN") or str(Path(GO2RTC_BIN).with_name("wyze-away" + Path(GO2RTC_BIN).suffix))
AWAY_DIR = Path(config.TOKEN_PATH) / "tailscale"
AWAY_PORT = int(os.getenv("FLASK_RUN_PORT", "5000"))
# wyze-away sets it on every request through the public no-app link (Tailscale Funnel).
PUBLIC_HEADER = "X-Wyze-Away-Public"

_codes: dict[str, float] = {}


def new_code() -> str:
    now = time.time()
    for code in [c for c, t in _codes.items() if t < now]:
        del _codes[code]
    code = secrets.token_urlsafe(18)
    _codes[code] = now + PAIR_SECONDS
    return code


def code_valid(code: str) -> bool:
    return _codes.get(code, 0) >= time.time()


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


class Away:
    """Away-from-home access through the bundled Tailscale: the web UI joins the user's private Tailscale network."""

    proc: Optional[Popen] = None

    @staticmethod
    def available() -> bool:
        return os.access(AWAY_BIN, os.X_OK)

    @staticmethod
    def on() -> bool:
        return (AWAY_DIR / "on").exists()

    @classmethod
    def running(cls) -> bool:
        return bool(cls.proc and cls.proc.poll() is None)

    @classmethod
    def turn(cls, on: bool) -> None:
        if on:
            AWAY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            (AWAY_DIR / "on").touch()
            cls.health_check()
            return
        # The sign-in stays in AWAY_DIR, so turning it back on needs no new sign-in.
        (AWAY_DIR / "on").unlink(missing_ok=True)
        if cls.running():
            cls.proc.terminate()
        cls.proc = None

    @classmethod
    def health_check(cls) -> None:
        """Starts the helper when away access is on, and again if it ever exits."""
        if cls.running():
            cls.check_sign_in()
        if not cls.on() or not cls.available() or cls.running():
            return
        if cls.proc:
            logger.error(f"[AWAY] wyze-away exited with code {cls.proc.returncode}, restarting")
        (AWAY_DIR / "status.json").unlink(missing_ok=True)
        # It exits when our end of its stdin closes, so it never outlives the bridge.
        args = [AWAY_BIN, "-dir", str(AWAY_DIR), "-port", str(AWAY_PORT), "-status", str(AWAY_DIR / "status.json"), "-funnel"]
        cls.proc = Popen(args, stdin=PIPE, stdout=DEVNULL)

    @classmethod
    def check_sign_in(cls) -> None:
        """One alert when Tailscale signs this computer out (its key expires every 180 days by default)."""
        state, flag = cls.status()["state"], AWAY_DIR / "signed-in"
        if state == "Running":
            flag.touch()
        elif state == "NeedsLogin" and flag.exists():
            flag.unlink()
            webhooks.send_webhook("away", "Wyze Tunnel", "Phones can't reach your cameras away from home until you sign in to Tailscale again. Open Login, then Pair a phone.")

    @classmethod
    def status(cls) -> dict:
        info = {"available": cls.available(), "on": cls.on(), "state": "", "auth_url": "", "host": "", "public": "", "funnel_url": ""}
        if info["on"] and cls.running():
            try:
                info |= json.loads((AWAY_DIR / "status.json").read_text())
            except (OSError, ValueError):
                info["state"] = "Starting"
        return info


def public_request() -> bool:
    """True when this request came over the public no-app link, where only paired phones may get in."""
    return PUBLIC_HEADER in request.headers


def tailscale_host() -> str:
    """This computer's Tailscale name, so a phone on Tailscale can reach it from anywhere. Empty if not set up."""
    if (away := Away.status())["state"] == "Running" and away["host"]:
        return away["host"]
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
