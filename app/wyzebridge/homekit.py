"""Apple Home: go2rtc's HomeKit server shows chosen cameras in the Home app. Turned on per camera on the Apps page."""
import hashlib
import json
import secrets
from pathlib import Path
from threading import Lock

import segno
import yaml
from wyzebridge.config import TOKEN_PATH

HOMEKIT_FILE = TOKEN_PATH + "homekit.json"
# Apple rejects these setup codes (same list as go2rtc's).
INSECURE_PINS = {str(n) * 8 for n in range(10)} | {"12345678", "87654321"}
CATEGORY_CAMERA = 17
_lock = Lock()


def _load() -> dict:
    try:
        with open(HOMEKIT_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _save(data: dict) -> None:
    # Holds each camera's HomeKit private key, so only the bridge's user can read it.
    path = Path(HOMEKIT_FILE)
    path.touch(mode=0o600)
    path.write_text(json.dumps(data))


def stream_id(uri: str) -> str:
    return f"{uri}_homekit"


def cameras() -> dict:
    return _load()


def set_enabled(uri: str, on: bool) -> None:
    """Turning a camera off forgets its pairing, so turning it on again makes a new accessory with a new code."""
    with _lock:
        data = _load()
        if on and uri not in data:
            while (pin := f"{secrets.randbelow(10**8):08d}") in INSECURE_PINS:
                pass
            mac = ":".join(f"{b:02X}" for b in secrets.token_bytes(6))
            data[uri] = {"pin": pin, "device_id": mac, "private": secrets.token_hex(32), "pairings": []}
        elif not on:
            data.pop(uri, None)
        _save(data)


def keep_pairings(config_path: str) -> None:
    """go2rtc writes new Apple Home pairings into its config file; copy them here before that file is rewritten."""
    try:
        with open(config_path) as f:
            written = (yaml.safe_load(f) or {}).get("homekit") or {}
    except (OSError, yaml.YAMLError):
        return
    with _lock:
        data = _load()
        changed = False
        for uri, cam in data.items():
            entry = written.get(stream_id(uri)) or {}
            pairings = entry.get("pairings")
            # Same device_id only: a camera turned off and on again is a new accessory without the old pairings.
            if entry.get("device_id") == cam["device_id"] and pairings is not None and pairings != cam["pairings"]:
                cam["pairings"], changed = pairings, True
        if changed:
            _save(data)


def go2rtc_config(names: dict[str, str]) -> tuple[dict, dict]:
    """go2rtc's streams and homekit sections for the cameras in Apple Home. names: uri to nickname."""
    streams, accessories = {}, {}
    for uri, cam in _load().items():
        if uri not in names:
            continue
        # From MediaMTX, so it works whichever way the camera connects, and starts the camera on demand.
        streams[stream_id(uri)] = f"rtsp://127.0.0.1:8554/{uri}"
        accessories[stream_id(uri)] = {
            "pin": cam["pin"],
            "name": names[uri][:64],
            # Random, since go2rtc would otherwise derive both from the stream name. The private key isn't
            # 64 bytes of hex, so go2rtc uses it as a secret seed.
            "device_id": cam["device_id"],
            "device_private": cam["private"],
            "pairings": cam["pairings"],
        }
    return streams, accessories


def setup_uri(uri: str, pin: str) -> str:
    """The X-HM:// link in Apple's setup QR code."""
    digest = hashlib.sha512(stream_id(uri).encode()).digest()
    setup_id = f"{digest[44]:02X}{digest[46]:02X}"  # same as go2rtc's calcSetupID, which isn't configurable
    payload = CATEGORY_CAMERA << 31 | 2 << 27 | int(pin)  # 2: connects over IP (Wi-Fi or Ethernet)
    code = ""
    while payload:
        payload, d = divmod(payload, 36)
        code = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"[d] + code
    return f"X-HM://{code:0>9}{setup_id}"


def pin_text(pin: str) -> str:
    return f"{pin[:3]}-{pin[3:5]}-{pin[5:]}"


def qr_svg(uri: str, pin: str) -> str:
    # Dark on white whatever the theme, so the Home app's scanner can read it.
    return segno.make(setup_uri(uri, pin), error="q").svg_inline(scale=4, border=2, dark="#111", light="#fff")
