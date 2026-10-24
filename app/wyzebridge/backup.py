"""Export and import the settings people change in the web UI. Wyze logins and tokens are never included."""
import base64
import hashlib
import json
import os
from datetime import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from wyzebridge import privacy, webhooks, wyze_api, wyze_stream
from wyzebridge.config import VERSION


def _files() -> dict[str, str]:
    # Looked up at call time so tests (and TOKEN_PATH overrides) see the current paths.
    return {
        "alerts": webhooks.ALERTS_FILE,
        "hidden_cams": wyze_api.HIDDEN_CAMS_FILE,
        "cam_names": wyze_api.CAM_NAMES_FILE,
        "net_modes": wyze_stream.NET_MODES_FILE,
        "quality": wyze_stream.QUALITY_FILE,
        "privacy": privacy.PRIVACY_FILE,
    }


def export_settings() -> dict:
    settings = {}
    for name, path in _files().items():
        try:
            with open(path) as f:
                settings[name] = json.load(f)
        except (FileNotFoundError, ValueError):
            pass
    return {"wyze_tunnel_backup": 1, "version": VERSION, "settings": settings}


def _key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(passphrase.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)


def encrypt(backup: dict, passphrase: str) -> dict:
    """Lock a backup with a passphrase so it can be kept anywhere, even off-site."""
    if len(passphrase) < 8:
        raise ValueError("Use a passphrase of at least 8 characters.")
    salt, nonce = os.urandom(16), os.urandom(12)
    data = AESGCM(_key(passphrase, salt)).encrypt(nonce, json.dumps(backup).encode(), None)
    b64 = lambda b: base64.b64encode(b).decode()
    return {"wyze_tunnel_backup": 1, "encrypted": {"kdf": "scrypt", "salt": b64(salt), "nonce": b64(nonce), "data": b64(data)}}


def unlock(backup):
    """Return a plain backup. A locked one carries the typed passphrase beside it; plain ones pass through."""
    if not isinstance(backup, dict) or "encrypted" not in backup:
        return backup
    if not isinstance(passphrase := backup.get("passphrase"), str) or not passphrase:
        raise ValueError("This backup is locked. Type its passphrase, then choose the file again.")
    try:
        box = backup["encrypted"]
        salt, nonce, data = (base64.b64decode(box[k]) for k in ("salt", "nonce", "data"))
        return json.loads(AESGCM(_key(passphrase, salt)).decrypt(nonce, data, None))
    except InvalidTag:
        raise ValueError("That passphrase doesn't open this backup.") from None
    except (KeyError, TypeError, ValueError):
        raise ValueError("Part of the backup is damaged, so nothing was restored.") from None


def _str_map(value, allowed: set | None = None) -> dict[str, str]:
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ValueError
    if allowed is not None and not set(value.values()) <= allowed:
        raise ValueError
    return value


def _str_list(value) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError
    return value


def _alerts(value) -> dict:
    if not isinstance(value, dict) or value.get("mode", "schedule") not in webhooks.ALERT_MODES:
        raise ValueError
    return value


def _privacy(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError
    _str_list(value.get("cams", []))
    for key in ("start", "end"):
        if value.get("cams") or key in value:
            time.fromisoformat(str(value.get(key)))  # ValueError if missing or not HH:MM
    return value


def import_settings(backup: dict) -> list[str]:
    """Validate everything first, then write. Raises ValueError with a message for the user."""
    if not isinstance(backup, dict) or backup.get("wyze_tunnel_backup") != 1:
        raise ValueError("That file isn't a Wyze Tunnel settings backup.")
    settings = backup.get("settings")
    if not isinstance(settings, dict) or not set(settings) <= set(_files()):
        raise ValueError("The backup has settings this version doesn't know.")
    checked = {
        "hidden_cams": _str_list,
        "cam_names": lambda v: {k: n[:64] for k, n in _str_map(v).items()},
        "net_modes": lambda v: _str_map(v, set(wyze_stream.NET_MODE_CHOICES.values())),
        "quality": lambda v: _str_map(v, set(wyze_stream.QUALITY_CHOICES.values())),
        "alerts": _alerts,
        "privacy": _privacy,
    }
    try:
        clean = {name: checked[name](value) for name, value in settings.items()}
    except ValueError:
        raise ValueError("Part of the backup is damaged, so nothing was restored.") from None
    if "alerts" in clean:
        alerts = clean.pop("alerts")
        webhooks.save_alerts(alerts)  # validates and raises ValueError with its own message
        if mode := alerts.get("mode"):
            webhooks.set_alert_mode(mode)
    files = _files()
    for name, value in clean.items():
        with open(files[name], "w") as f:
            json.dump(value, f)
    return sorted(settings)
