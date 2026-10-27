"""Doorbell on the TV: when the doorbell rings, picked Chromecasts, Google TVs and Nest Hubs show the visitor's picture
and say who's there out loud.

PyChromecast (MIT, github.com/home-assistant-libs/pychromecast) finds and drives the devices. Piper (GPL-3.0,
github.com/OHF-Voice/piper1-gpl) speaks the sentence on this computer, with the LibriTTS-R voice (CC BY 4.0),
downloaded once. A cast device can't sign in, so it fetches the sound and picture from secret links made for each ring
that expire after two minutes. It needs to reach this computer's web page, so it's off under Tailscale only.
"""

import io
import json
import os
import secrets
import time
import wave
from pathlib import Path
from threading import Lock

from wyzebridge import lockdown
from wyzebridge.config import IMG_PATH, TOKEN_PATH
from wyzebridge.detect import MODEL_DIR
from wyzebridge.go2rtc import local_ipv4s
from wyzebridge.logging import logger

try:
    import pychromecast
    from piper import PiperVoice
    from piper.download_voices import download_voice
except ImportError:
    pychromecast = None

SETTINGS_FILE = TOKEN_PATH + "cast.json"
VOICE = "en_US-libritts_r-medium"
EXPIRE = 120
_media: dict[str, tuple[float, bytes, Path]] = {}  # token -> (expires, wav, picture)
_lock = Lock()
_voice = None


def available() -> bool:
    return pychromecast is not None


def load_settings() -> dict:
    """{"devices": [friendly name, ...]} picked on the Alerts page."""
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save_settings(devices) -> list[str]:
    """Raises ValueError with a message for the user."""
    if not isinstance(devices, list) or not all(isinstance(d, str) and 0 < len(d) <= 100 for d in devices):
        raise ValueError("Pick devices from the list")
    if devices and not available():
        raise ValueError("Casting isn't available on this device")
    with open(SETTINGS_FILE, "w") as f:
        json.dump({"devices": devices}, f)
    return devices


def discover() -> list[str]:
    """Names of the cast devices on this network, after a 5 second look."""
    if not available():
        return []
    found, browser = pychromecast.discovery.discover_chromecasts(timeout=5)
    browser.stop_discovery()
    return sorted({c.friendly_name for c in found})


def speak(text: str) -> bytes:
    """A WAV file of the sentence. Downloads the voice (about 80 MB) the first time."""
    global _voice
    with _lock:
        if not _voice:
            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            download_voice(VOICE, MODEL_DIR)
            try:
                _voice = PiperVoice.load(MODEL_DIR / f"{VOICE}.onnx")
            except Exception:
                for ext in (".onnx", ".onnx.json"):  # a cut-off download: fetch it again next time
                    (MODEL_DIR / f"{VOICE}{ext}").unlink(missing_ok=True)
                raise
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            _voice.synthesize_wav(text, wav)
    return buf.getvalue()


def media(token: str, ext: str) -> tuple[bytes, str] | None:
    """What a cast device fetches: (body, content type), or None once used or expired."""
    now = time.time()
    for old in [t for t, (expires, *_) in _media.items() if expires < now]:
        _media.pop(old, None)
    if not (hit := _media.get(token)):
        return None
    if ext == "wav":
        return hit[1], "audio/wav"
    try:
        return hit[2].read_bytes(), "image/jpeg"
    except OSError:
        return None


def base_url() -> str:
    """This computer's address as a cast device on the network sees it."""
    ips = sorted(ip for ip in local_ipv4s() if not ip.startswith("127."))
    return f"http://{ips[0]}:{os.getenv('FLASK_RUN_PORT', '5000')}" if ips else ""


def announce(uri: str, text: str) -> None:
    """Run in a thread after a ring: say `text` with the doorbell's latest picture on every picked device."""
    names = load_settings().get("devices") or []
    if not names or not available() or lockdown.tailscale_only() or not (base := base_url()):
        return
    try:
        token = secrets.token_urlsafe(16)
        _media[token] = (time.time() + EXPIRE, speak(text), Path(IMG_PATH, f"{uri}.jpg"))
        casts, browser = pychromecast.get_listed_chromecasts(friendly_names=names, discovery_timeout=5)
        try:
            for cc in casts:
                cc.wait(timeout=10)
                cc.media_controller.play_media(f"{base}/cast/{token}.wav", "audio/wav", title=text,
                                               thumb=f"{base}/cast/{token}.jpg", stream_type="BUFFERED")
            logger.info(f"[CAST] Told {len(casts)} of {len(names)} devices about {uri}")
        finally:
            browser.stop_discovery()
    except Exception as ex:  # a missing TV or a failed voice download must never break the ring alert
        logger.warning(f"[CAST] {ex}")
