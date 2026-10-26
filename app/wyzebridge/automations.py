"""Automations across devices. Each is off until the owner turns it on, and none of them ever locks or unlocks a door."""

import json
import os
import threading
import time

from wyzebridge import devices
from wyzebridge.config import TOKEN_PATH
from wyzebridge.logging import logger
from wyzecam.api import AccessTokenError

SETTINGS_FILE = TOKEN_PATH + "automations.json"
STATE_FILE = TOKEN_PATH + "automations_state.json"
POLL_SECONDS = devices.CACHE_SECONDS  # the devices cache is a minute old anyway, so asking more often only costs Wyze calls
_lock = threading.Lock()
_open_since: dict[str, float] = {}  # shortcut: kept in memory, so a restart starts the open-door timer over
_last_poll = {"at": 0.0}


def _read(path: str) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _write(path: str, data: dict) -> None:
    with open(path + ".tmp", "w") as f:
        json.dump(data, f)
    os.replace(path + ".tmp", path)


def load() -> dict:
    return _read(SETTINGS_FILE)


def _state() -> dict:
    """What the automations changed and must undo, so a restart in the middle still undoes it."""
    return _read(STATE_FILE)


def _minutes(value, default: int) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).strip().isdigit() or not 1 <= int(value) <= 60:
        raise ValueError("Minutes must be a whole number from 1 to 60.")
    return int(value)


def _macs(value, allowed: dict, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(m, str) and m in allowed for m in value):
        raise ValueError(f"Pick {what} from the list.")
    return list(dict.fromkeys(value))


def _heat(form: dict, cams: set, devs: dict) -> dict:
    on = form.get("on") is True
    sensors = _macs(form.get("sensors") or [], {m: t for m, t in devs.items() if t == "ContactSensor"}, "door and window sensors")
    if on and not sensors:
        raise ValueError("Pick at least one door or window sensor.")
    if on and "Thermostat" not in devs.values():
        raise ValueError("There's no thermostat on this Wyze account.")
    return {"on": on, "sensors": sensors, "minutes": _minutes(form.get("minutes"), 2)}


SECTIONS = {"heat": _heat}


def save(form: dict, cams: set, devs: dict) -> dict:
    """Validate and save the sections in the form. cams are camera macs, devs maps device mac to type. ValueError for the user."""
    if not isinstance(form, dict) or not form or not set(form) <= set(SECTIONS) or not all(isinstance(v, dict) for v in form.values()):
        raise ValueError("Nothing to save.")
    settings = load() | {name: SECTIONS[name](value, cams, devs) for name, value in form.items()}
    _write(SETTINGS_FILE, settings)
    return settings


def _call(api, fn, *args):
    """Retries once with a fresh token if it expired."""
    try:
        return fn(api.auth, *args)
    except AccessTokenError:
        api.refresh_token()
        return fn(api.auth, *args)


def _pause_heat(api, cfg: dict, state: dict, now: float) -> None:
    """Heat off after a door has been open a while; the exact old mode back once everything is closed."""
    paused = state.get("heat")
    if not cfg.get("on") and not paused:
        return
    found = _call(api, devices.list_devices)
    thermo = next((d for d in found if d["mac"] == paused["mac"]), None) if paused else next((d for d in found if d["model"] == "CO_EA1"), None)
    if not thermo:
        return
    open_now = {d["mac"] for d in found if d["mac"] in cfg.get("sensors", []) and d["state"].get("open")}
    for mac in set(_open_since) - open_now:
        del _open_since[mac]
    for mac in open_now:
        _open_since.setdefault(mac, now)
    mode = thermo["state"].get("mode_sys")
    if paused:
        if (open_now and cfg.get("on")) or mode is None:  # still open, or unreadable: look again next time
            return
        if mode == "off":
            _call(api, devices.control, thermo, {"mode": paused["mode"]})
            logger.warning(f"[AUTOMATIONS] Doors closed: {thermo['name']} back to {paused['mode']}")
        else:
            logger.warning(f"[AUTOMATIONS] {thermo['name']} was changed to {mode} during the pause; leaving it alone")
        state.pop("heat")
        _write(STATE_FILE, state)
    elif mode in devices.MODES - {"off"} and any(now - since >= cfg["minutes"] * 60 for since in _open_since.values()):
        state["heat"] = {"mac": thermo["mac"], "mode": mode, "at": now}
        _write(STATE_FILE, state)  # before the change, so a crash in between still restores
        try:
            _call(api, devices.control, thermo, {"mode": "off"})
        except Exception:
            state.pop("heat")
            _write(STATE_FILE, state)
            raise
        logger.warning(f"[AUTOMATIONS] A door has been open {cfg['minutes']} min: {thermo['name']} off (was {mode})")


def tick(api, streams: dict, now: float | None = None) -> None:
    """Run every few seconds from the stream monitor. One at a time; a slow Wyze call just skips the next tick."""
    if not _lock.acquire(blocking=False):
        return
    try:
        now = time.time() if now is None else now
        settings, state = load(), _state()
        if now - _last_poll["at"] >= POLL_SECONDS:
            _last_poll["at"] = now
            try:
                _pause_heat(api, settings.get("heat") or {}, state, now)
            except Exception as ex:
                logger.warning(f"[AUTOMATIONS] Heat pause: {ex}")
    finally:
        _lock.release()
