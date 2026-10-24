"""Export and import the settings people change in the web UI. Wyze logins and tokens are never included."""
import json

from wyzebridge import webhooks, wyze_api, wyze_stream
from wyzebridge.config import VERSION


def _files() -> dict[str, str]:
    # Looked up at call time so tests (and TOKEN_PATH overrides) see the current paths.
    return {
        "alerts": webhooks.ALERTS_FILE,
        "hidden_cams": wyze_api.HIDDEN_CAMS_FILE,
        "cam_names": wyze_api.CAM_NAMES_FILE,
        "net_modes": wyze_stream.NET_MODES_FILE,
        "quality": wyze_stream.QUALITY_FILE,
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
