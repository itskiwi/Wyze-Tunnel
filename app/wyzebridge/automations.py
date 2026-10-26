"""Automations across devices. Each is off until the owner turns it on, and none of them ever locks or unlocks a door."""

import json
import math
import os
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

from wyzebridge import devices, weather
from wyzebridge.bridge_utils import env_bool
from wyzebridge.config import TOKEN_PATH
from wyzebridge.logging import logger
from wyzebridge.webhooks import send_webhook
from wyzebridge.wyze_api import display_name
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


LIGHT_TYPES = {"OutdoorPlug", "Plug", "MeshLight"}


def _lights(form: dict, cams: set, devs: dict) -> dict:
    on = form.get("on") is True
    cameras = _macs(form.get("cameras") or [], dict.fromkeys(cams), "cameras")
    lights = _macs(form.get("devices") or [], {m: t for m, t in devs.items() if t in LIGHT_TYPES}, "plugs and lights")
    if on and not (cameras and lights):
        raise ValueError("Pick at least one camera and one plug or light.")
    return {"on": on, "cameras": cameras, "devices": lights, "minutes": _minutes(form.get("minutes"), 5), "also_motion": form.get("also_motion") is True}


def _bedtime(form: dict, cams: set, devs: dict) -> dict:
    try:
        at = datetime.strptime(str(form.get("time") or "22:00").strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        raise ValueError("Bedtime should be a time like 22:30.") from None
    return {"on": form.get("on") is True, "time": at, "silent": form.get("silent") is True}


SECTIONS = {"heat": _heat, "lights": _lights, "bedtime": _bedtime}


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


def is_night(ts: float) -> bool:
    """After sunset and before sunrise at the saved home spot (NOAA's approximation, good to a few minutes). 19:00 to 06:00 if unknown."""
    if not (loc := weather.location()):
        hour = datetime.fromtimestamp(ts).hour
        return hour >= 19 or hour < 6
    d = datetime.fromtimestamp(ts, timezone.utc)
    g = 2 * math.pi / 365 * (d.timetuple().tm_yday - 1 + (d.hour - 12) / 24)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g) - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    lat = math.radians(loc["lat"])
    cos_ha = math.cos(math.radians(90.833)) / (math.cos(lat) * math.cos(decl)) - math.tan(lat) * math.tan(decl)
    if abs(cos_ha) >= 1:  # polar night or midnight sun
        return cos_ha > 0
    ha = math.degrees(math.acos(cos_ha))
    rise = (720 - 4 * (loc["lon"] + ha) - eqtime) % 1440
    sset = (720 - 4 * (loc["lon"] - ha) - eqtime) % 1440
    minute = d.hour * 60 + d.minute
    # In UTC the sun can set "tomorrow", so the daylight span may wrap past midnight.
    return not (rise <= minute < sset if rise < sset else minute >= rise or minute < sset)


def on_motion(api, cam_mac: str, label: str, now: float | None = None) -> None:
    """A camera saw something. At night, switch the chosen lights on for a while; later sightings extend it."""
    cfg = load().get("lights") or {}
    if not cfg.get("on") or cam_mac not in cfg.get("cameras", []):
        return
    if label != "person" and not (label == "motion" and cfg.get("also_motion")):
        return
    now = time.time() if now is None else now
    if not is_night(now):
        return
    with _lock:
        state = _state()
        mine = state.setdefault("lights", {})
        found = None
        try:
            for mac in cfg["devices"]:
                if mac in mine:  # ours already: just more time
                    mine[mac]["off_at"] = now + cfg["minutes"] * 60
                    continue
                found = found or {d["mac"]: d for d in _call(api, devices.list_devices, True)}
                dev = found.get(mac)
                if not dev or dev["state"].get("on") is not False:  # gone, unreadable or already on: not ours to turn off
                    continue
                _call(api, devices.control, dev, {"on": True})
                mine[mac] = {"off_at": now + cfg["minutes"] * 60, "name": dev["name"], "type": dev["type"], "model": dev["model"]}
                logger.warning(f"[AUTOMATIONS] Someone is outside: {dev['name']} on for {cfg['minutes']} min")
        except Exception as ex:
            logger.warning(f"[AUTOMATIONS] Lights: {ex}")
        _write(STATE_FILE, state)


def _lights_off(api, state: dict, now: float) -> None:
    for mac, light in list(state.get("lights", {}).items()):
        if now < light["off_at"]:
            continue
        try:
            _call(api, devices.control, {"mac": mac, "type": light["type"], "model": light["model"]}, {"on": False})
            del state["lights"][mac]
            logger.warning(f"[AUTOMATIONS] {light['name']} off again")
        except Exception as ex:
            light["off_at"] = now + 60
            logger.warning(f"[AUTOMATIONS] Couldn't turn {light['name']} off, trying again in a minute: {ex}")
        _write(STATE_FILE, state)


def problems(found: list[dict], streams: dict) -> list[str]:
    """What's wrong with the house right now, one line each. Empty when all is well."""
    out = []
    for d in found:
        s, name = d["state"], d["name"]
        if s.get("error"):
            out.append(f"Couldn't check {name}")
        if s.get("locked") is False:
            out.append(f"{name} is unlocked")
        if d["type"] == "ContactSensor" and s.get("open"):
            out.append(f"{name} is open")
        battery = s.get("battery")
        if s.get("low_battery") or (d["type"] == "Lock" and isinstance(battery, int) and battery <= 20):
            out.append(f"{name} has a low battery")
    out += [f"{display_name(st.camera)} is offline" for st in streams.values() if st.enabled and not st.options.substream and st.state == -90]
    return out


def _bedtime_check(api, streams: dict, cfg: dict, state: dict, now: datetime) -> None:
    """Once an evening, at the owner's bedtime (a restart later than two hours after it doesn't send)."""
    if not cfg.get("on") or state.get("bedtime_day") == str(now.date()):
        return
    start = datetime.combine(now.date(), datetime.strptime(cfg["time"], "%H:%M").time())
    if not start <= now < start + timedelta(hours=2):
        return
    found = _call(api, devices.list_devices, True)  # raises when Wyze can't be read, so it tries again next time
    state["bedtime_day"] = str(now.date())
    _write(STATE_FILE, state)
    issues = problems(found, streams)
    if not issues and cfg.get("silent"):
        return
    # shortcut: assumes the default port; set DOMAIN if the bridge is reached some other way.
    link = f"http://{env_bool('DOMAIN', socket.gethostname())}:5000/devices"
    body = "\n".join(f"- {i}" for i in issues) if issues else "All locked up."
    send_webhook("bedtime", "all", f"{body}\nDevices: {link}")


def tick(api, streams: dict, now: float | None = None) -> None:
    """Run every few seconds from the stream monitor. One at a time; a slow Wyze call just skips the next tick."""
    if not _lock.acquire(blocking=False):
        return
    try:
        now = time.time() if now is None else now
        settings, state = load(), _state()
        _lights_off(api, state, now)
        try:
            _bedtime_check(api, streams, settings.get("bedtime") or {}, state, datetime.fromtimestamp(now))
        except Exception as ex:
            logger.warning(f"[AUTOMATIONS] Bedtime check: {ex}")
        if now - _last_poll["at"] >= POLL_SECONDS:
            _last_poll["at"] = now
            try:
                _pause_heat(api, settings.get("heat") or {}, state, now)
            except Exception as ex:
                logger.warning(f"[AUTOMATIONS] Heat pause: {ex}")
    finally:
        _lock.release()
