"""Export and import the settings people change in the web UI. Wyze logins and tokens are never included."""
import base64
import hashlib
import json
import os
import time as clock
from datetime import date, time
from pathlib import Path

import yaml
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from wyzebridge import detect, masks, phrases, privacy, timelapse, weather, webhooks, wyze_api, wyze_stream
from wyzebridge.config import TOKEN_PATH, VERSION
from wyzebridge.logging import logger

AUTO_FILE = TOKEN_PATH + "auto_backup.json"
AUTO_KEEP = 7  # daily files kept in the folder


def _files() -> dict[str, str]:
    # Looked up at call time so tests (and TOKEN_PATH overrides) see the current paths.
    return {
        "alerts": webhooks.ALERTS_FILE,
        "hidden_cams": wyze_api.HIDDEN_CAMS_FILE,
        "cam_names": wyze_api.CAM_NAMES_FILE,
        "cam_places": phrases.PLACES_FILE,
        "net_modes": wyze_stream.NET_MODES_FILE,
        "quality": wyze_stream.QUALITY_FILE,
        "privacy": privacy.PRIVACY_FILE,
        "battery_saver": privacy.BATTERY_FILE,
        "timelapse": timelapse.SETTINGS_FILE,
        "zones": detect.ZONES_FILE,
        "masks": masks.MASKS_FILE,
        "location": weather.LOCATION_FILE,
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


def _zones(value) -> dict:
    cells = range(detect.HEAT_W * detect.HEAT_H)
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, list) and all(isinstance(i, int) and i in cells for i in v) for k, v in value.items()
    ):
        raise ValueError
    return value


def _location(value) -> dict:
    if not isinstance(value, dict) or not all(isinstance(value.get(k, 0), (int, float)) for k in ("lat", "lon")):
        raise ValueError
    return {k: value[k] for k in ("lat", "lon", "name", "unit", "weather") if k in value}


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
        "cam_places": lambda v: _str_map(v, set(phrases.PLACE_CHOICES)),
        "net_modes": lambda v: _str_map(v, set(wyze_stream.NET_MODE_CHOICES.values())),
        "quality": lambda v: _str_map(v, set(wyze_stream.QUALITY_CHOICES.values())),
        "alerts": _alerts,
        "privacy": _privacy,
        "battery_saver": _privacy,
        "zones": _zones,
        "masks": _zones,
        "location": _location,
        "timelapse": lambda v: {"cams": _str_list(v.get("cams", []) if isinstance(v, dict) else None)},
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


def load_auto() -> dict:
    try:
        with open(AUTO_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _write_auto(settings: dict) -> None:
    # Holds the passphrase, so only the bridge's own user may read it.
    with open(os.open(AUTO_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(settings, f)


def save_auto(form: dict) -> dict:
    """Turn the daily locked backup on (a folder and passphrase) or off (an empty folder)."""
    folder = str(form.get("folder") or "").strip()
    if not folder:
        _write_auto({})
        return {}
    if not os.path.isabs(folder) or not os.path.isdir(folder) or not os.access(folder, os.W_OK):
        raise ValueError("Pick a folder that already exists and that Wyze Tunnel can write to, like /media/backups.")
    passphrase = str(form.get("passphrase") or "") or load_auto().get("passphrase", "")
    encrypt({}, passphrase)  # raises the same "at least 8 characters" message
    settings = {"folder": folder, "passphrase": passphrase, "last": 0}
    _write_auto(settings)
    return settings


def auto_backup(force: bool = False, now: float | None = None) -> str | None:
    """Write today's locked backup if one is due, keep the newest few, and return its path."""
    settings = load_auto()
    now = clock.time() if now is None else now
    if not settings.get("folder") or (not force and now - settings.get("last", 0) < 86400):
        return None
    folder = Path(settings["folder"])
    path = folder / f"wyze-tunnel-settings-{date.fromtimestamp(now).isoformat()}.json"
    try:
        path.write_text(json.dumps(encrypt(export_settings(), settings["passphrase"])))
        for old in sorted(folder.glob("wyze-tunnel-settings-*.json"))[:-AUTO_KEEP]:
            old.unlink()
    except OSError as ex:
        if force:
            raise ValueError(f"Couldn't save to {folder}: {ex.strerror}") from None
        logger.warning(f"[BACKUP] Couldn't save to {folder}: {ex}. Trying again in an hour.")
        _write_auto({**settings, "last": now - 86400 + 3600})
        return None
    _write_auto({**settings, "last": now})
    logger.info(f"[BACKUP] Saved a locked settings backup to {path}")
    return str(path)


DWB_SIGN_IN = {"WYZE_EMAIL", "WYZE_PASSWORD", "API_ID", "API_KEY", "TOTP_KEY"}


def _dwb_pairs(text: str) -> list[tuple[str, str]]:
    """Name/value pairs from a docker-compose file (every service's environment) or a plain .env file."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        doc = None
    if isinstance(doc, dict) and isinstance(doc.get("services"), dict):
        pairs = []
        for service in doc["services"].values():
            env = (service or {}).get("environment") if isinstance(service, dict) else None
            if isinstance(env, dict):
                pairs += [(str(k), "" if v is None else str(v)) for k, v in env.items()]
            elif isinstance(env, list):
                pairs += [tuple(str(line).split("=", 1)) for line in env if "=" in str(line)]
        return pairs
    lines = (line.strip().removeprefix("export ").strip() for line in text.splitlines())
    return [tuple(line.split("=", 1)) for line in lines if "=" in line and not line.startswith("#")]


def from_docker_wyze_bridge(text: str) -> dict:
    """Turn docker-wyze-bridge settings into a wyze-tunnel.env. Option names are the same; only what can't move is left out."""
    if len(text) > 100_000:
        raise ValueError("That's too long for a settings file. Paste just the docker-compose.yml or .env.")
    keep, skipped = {}, []
    for name, value in _dwb_pairs(text):
        name, value = name.strip().upper(), value.strip().strip("'\"")
        if not name.replace("_", "").isalnum():
            continue
        if name in DWB_SIGN_IN:
            skipped.append({"name": name, "why": "Sign in from the setup page instead, so it isn't saved in a plain file."})
        elif "${" in value:
            skipped.append({"name": name, "why": "Its value comes from another file. Copy the real value in by hand."})
        elif value.startswith("/"):
            skipped.append({"name": name, "why": "It points to a folder inside the old Docker container."})
        else:
            keep[name] = value
    if not keep and not skipped:
        raise ValueError("No settings found. Paste your docker-compose.yml or .env file.")
    env = "# Moved from docker-wyze-bridge\n" + "".join(f"{k}={v}\n" for k, v in keep.items())
    return {"env": env, "kept": list(keep), "skipped": skipped}
