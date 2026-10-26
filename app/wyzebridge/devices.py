"""Wyze plugs, bulbs, sensors, thermostat, lock and chime: read state and send simple commands.

Endpoints come from the Wyze Android app (v3.24); file references are to its decompiled sources.
"""

import json
import os
import time
from hashlib import md5
from threading import Thread
from typing import Any
from urllib.parse import quote_plus

from requests import RequestException
from wyzebridge.config import TOKEN_PATH
from wyzebridge.logging import logger
from wyzebridge.webhooks import ALERTS, send_webhook
from wyzecam.api import (
    WYZE_API,
    AccessTokenError,
    WyzeAPIError,
    _headers,
    _payload,
    get,
    post,
    post_device,
    sign_payload,
    sort_dict,
    validate_resp,
)
from wyzecam.api_models import WyzeCredential

# com/wyze/sensor/SensorApi.java:28 (thermostat and its room sensors)
EARTH_API = "https://wyze-earth-service.wyzecam.com/plugin/earth"
# certifi dropped DigiCert Global Root CA (Mozilla distrust, 2026) but the earth service still chains to it; the OS store keeps it.
# shortcut: falls back to certifi where neither file exists, so the thermostat reads fail there until Wyze reissues the cert.
EARTH_CA = next((p for p in ("/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/cert.pem") if os.path.exists(p)), True)
# com/yunding/commonbiz/constant/HttpUrl.java getSaasServerUrl(); key and secret: CommonBizConstants.java:59-60
FORD_API = "https://yd-saas-toc.wyzecam.com"
FORD_KEY, FORD_SECRET = "275965684684dbdaf29a0ed9", "4deekof1ba311c5c33a9cb8e12787e8c"
# The earth service takes the main app's signature2, like post_device(api_version=4).
APP_ID = "9319141212m2ik"
THERMOSTAT_KEYS = "temperature,humidity,mode_sys,heat_sp,cool_sp,temp_unit,working_state,iot_state"
ROOM_KEYS = "temperature,humidity,battery,temperature_unit"  # SensorHomeActivity.java:399, SensorIotSettingActivity.java:71
BULB_PIDS = {"P3": "on", "P1501": "brightness", "P1502": "color_temp", "P1507": "color"}
MODES = {"auto", "heat", "cool", "off"}
HIDDEN = {"Camera"}
# The outdoor plug's own row duplicates its two outlets (WLPPO-SUB), which are what the app switches.
HIDDEN_MODELS = {"WLPPO"}
CACHE_SECONDS = 60
LOG_CACHE_SECONDS = 120
HISTORY_FILE = TOKEN_PATH + "device_history.json"
HISTORY_STEP = 300  # one reading per device every 5 minutes, kept 7 days
HISTORY_MODELS = {"CO_EA1", "CO_TH1"}
WATCH_SECONDS = 180
# Titles from the app's resources.arsc (strings yunding_lock_history_event_id_*, _source_*), picked by HomeListAdapter.java:163-199.
# 2217 has no string in the app. 2218 appends the open-alarm time, see _lock_event.
LOCK_EVENTS = {2201: "Multiple failed code unlock attempts", 2203: "Unlocked", 2214: "Opened", 2215: "Closed", 2216: "Locked",
               2218: "Open too long", 2221: "The door is jammed", 2222: "The door is totally jammed", 2223: "The door swung open",
               2224: "The door is kept open for more than 24 hours", 2225: "Trash Mode is on", 2226: "Door auto-calibrated",
               2227: "Multiple failed code unlock attempts", 2228: "Multiple failed fingerprint unlock attempts"}
# Only 2203/2216 carry a source (HomeListAdapter.java:337-338). Bolt and gun safe call source 2 "Code"; the Wyze Lock says "Keypad".
LOCK_SOURCES = {1: "App", 2: "Keypad", 3: "Fingerprint", 4: "Inside button", 5: "Manual", 6: "Inside holder", 7: "NFC", 8: "Auto", 9: "Remote", 12: "Remote", 102: "Keypad"}
OPEN_ALARM_MINUTES = (1, 5, 10, 30, 60)  # array yunding_open_alarm_time1, indexed by detail.left_open_time - 2 (HomeListAdapter.java:372)

_cache: dict[str, Any] = {"at": 0.0, "devices": []}
_lock_cache: dict[str, tuple[float, dict]] = {}
_usage_cache: dict[str, tuple[float, list]] = {}
_unlocked: dict[str, float] = {}  # lock mac -> when we first saw it unlocked
_alerted: set[str] = set()


def _load_history() -> dict[str, list[dict]]:
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


_history: dict[str, list[dict]] = _load_history()


def list_devices(auth: WyzeCredential, fresh: bool = False) -> list[dict]:
    """Every non-camera device with its state. Cached for a minute so the page can't burn the rate limit."""
    if not fresh and _cache["devices"] and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["devices"]
    data = validate_resp(post(f"{WYZE_API}/v2/home_page/get_object_list", json=_payload(auth), headers=_headers()))
    devices = [_device(auth, d) for d in data["device_list"] if d["product_type"] not in HIDDEN and d["product_model"] not in HIDDEN_MODELS]
    _cache.update(at=time.time(), devices=devices)
    record_history(devices)
    lock_alert(devices)
    return devices


def find(auth: WyzeCredential, mac: str) -> dict | None:
    return next((d for d in list_devices(auth) if d["mac"] == mac), None)


def _device(auth: WyzeCredential, d: dict) -> dict:
    params = d.get("device_params") or {}
    dev = {"mac": d["mac"], "model": d["product_model"], "type": d["product_type"], "name": d.get("nickname") or d["mac"],
           "online": d.get("conn_state") == 1, "parent": d.get("parent_device_mac") or "", "state": {}}
    state = dev["state"]
    if "switch_state" in params and dev["type"] != "Lock":
        state["on"] = params["switch_state"] == 1
    if "open_close_state" in params:
        state["open"] = params["open_close_state"] == 1
    if "motion_state" in params:
        state["motion"] = params["motion_state"] == 1
    if "is_low_battery" in params:
        state["low_battery"] = params["is_low_battery"] == 1
    try:
        if dev["model"] == "CO_EA1":
            state |= earth_props(auth, dev["mac"], THERMOSTAT_KEYS)
        elif dev["model"] == "CO_TH1":
            state |= earth_props(auth, dev["mac"], ROOM_KEYS, {"is_sub_device": "true", "model": "CO_TH1"})
            if "temperature" in state:  # hundredths of a degree, e.g. 6749
                state["temperature"] = str(int(state["temperature"]) / 100)
        elif dev["type"] == "Lock":
            state |= lock_info(auth, dev["mac"])
        elif dev["type"] == "MeshLight" and dev["online"]:
            state |= bulb_props(auth, dev["mac"], dev["model"])
    except (WyzeAPIError, RequestException, KeyError, ValueError) as ex:
        state["error"] = f"Couldn't read: {ex}"
    return dev


def earth_props(auth: WyzeCredential, did: str, keys: str, extra: dict | None = None) -> dict:
    """GET /plugin/earth/get_iot_prop. Signature2 signs the sorted k=v query (WpkWyzeExService.java:246-250)."""
    params = {"did": did, "keys": keys, "nonce": str(int(time.time() * 1000))} | (extra or {})
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    resp = get(f"{EARTH_API}/get_iot_prop", params=params, headers=sign_payload(auth, APP_ID, query), verify=EARTH_CA)
    return validate_resp(resp)["props"]


def set_thermostat(auth: WyzeCredential, did: str, props: dict) -> None:
    """POST /plugin/earth/set_iot_prop_by_topic; Signature2 signs the exact JSON body."""
    body = sort_dict({"did": did, "model": "CO_EA1", "props": props, "is_sub_device": 0, "nonce": str(int(time.time() * 1000))})
    validate_resp(post(f"{EARTH_API}/set_iot_prop_by_topic", data=body, headers=sign_payload(auth, APP_ID, body), verify=EARTH_CA))


def bulb_props(auth: WyzeCredential, mac: str, model: str) -> dict:
    resp = post_device(auth, "get_device_Info", {"device_mac": mac, "device_model": model}, api_version=2)
    props = {BULB_PIDS[p["pid"]]: p["value"] for p in resp["property_list"] if p["pid"] in BULB_PIDS}
    if "on" in props:
        props["on"] = props["on"] == "1"
    return props


def set_bulb(auth: WyzeCredential, mac: str, model: str, plist: list[dict]) -> None:
    """Mesh bulbs take set_mesh_property through run_action_list (sp6.java:430-437, WpkAutoManager.java:166)."""
    action = {"action_key": "set_mesh_property", "action_params": {"list": [{"mac": mac, "plist": plist}]},
              "instance_id": mac, "provider_key": model}
    payload = _payload(auth) | {"sv": "5e02224ae0c64d328154737602d28833", "action_list": [action], "custom_string": ""}
    validate_resp(post(f"{WYZE_API}/v2/auto/run_action_list", json=payload, headers=_headers()))


def set_plug(auth: WyzeCredential, mac: str, model: str, on: bool) -> None:
    """Outlets switch with P3 on their own mac (com/hualai/wlppo/d.java:101)."""
    post_device(auth, "set_property", {"device_mac": mac, "device_model": model, "pid": "P3", "pvalue": "1" if on else "0"}, api_version=2)


def ford_sign(method: str, path: str, params: dict) -> str:
    """md5 of the URL-encoded method+path+sorted params+secret (com/yunding/commonbiz/util/HttpApi.java getSignStr)."""
    raw = method + path + "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v != "") + FORD_SECRET
    return md5(quote_plus(raw).encode()).hexdigest()


def _ford(auth: WyzeCredential, method: str, path: str, params: dict) -> dict:
    params = params | {"access_token": auth.access_token, "key": FORD_KEY, "timestamp": str(int(time.time() * 1000))}
    params["sign"] = ford_sign(method, path, params)
    # HttpApi.java sends GET and POST params as query/form fields, not JSON.
    call = get(FORD_API + path, params=params) if method == "get" else post(FORD_API + path, data=params)
    data = validate_resp(call)
    if data.get("ErrNo", 0) != 0:
        raise WyzeAPIError(data["ErrNo"], data.get("ErrMsg", ""), call.request)
    return data


def lock_uuid(mac: str) -> str:
    return mac.split(".")[-1]


def lock_info(auth: WyzeCredential, mac: str) -> dict:
    """GET /openapi/lock/v1/info. door 1 open / 2 closed (LockHomeHeader.java:88-95); hardlock 2 is unlocked."""
    dev = _ford(auth, "get", "/openapi/lock/v1/info", {"uuid": lock_uuid(mac)})["device"]
    status = dev.get("locker_status") or {}
    return {"locked": status.get("hardlock") != 2, "open": status.get("door") == 1, "battery": dev.get("power"),
            "lock_online": dev.get("onoff_line") == 1}


def set_lock(auth: WyzeCredential, mac: str, locked: bool) -> None:
    """POST /openapi/lock/v1/control (FordHttpUrl.java:35, NetLockManager.remoteControlLock)."""
    _ford(auth, "post", "/openapi/lock/v1/control", {"uuid": lock_uuid(mac), "action": "remoteLock" if locked else "remoteUnlock"})


def lock_log(auth: WyzeCredential, mac: str, fresh: bool = False) -> dict:
    """Recent lock activity and the keypad code list, read-only. Cached so opening the panel can't hammer Wyze.

    GET /openapi/v1/safety/family_record (HttpUrl.java:34, NetLockManager.java:53-90; time is in ms, FamilyRecord.java) and
    GET /openapi/lock/v1/pwd with detail=1 (KeyManager.java:505-532, LockKeyEntity.java). Only allow-listed fields are copied:
    the code digits (LockPasswordInfo.pwdValue) and the people's emails and avatars never leave this function.
    """
    if not fresh and (hit := _lock_cache.get(mac)) and time.time() - hit[0] < LOG_CACHE_SECONDS:
        return hit[1]
    uuid = lock_uuid(mac)
    records = _ford(auth, "get", "/openapi/v1/safety/family_record", {"uuid": uuid, "offset": "0", "limit": "20"}).get("family_record") or []
    codes = _ford(auth, "get", "/openapi/lock/v1/pwd", {"uuid": uuid, "detail": "1"}).get("passwords") or []
    out = {"events": [_lock_event(r) for r in records], "codes": [_keypad_code(c) for c in codes]}
    _lock_cache[mac] = (time.time(), out)
    return out


def _lock_event(r: dict) -> dict:
    d = r.get("detail") or {}
    eid = r.get("eventid")
    event = LOCK_EVENTS.get(eid, f"Event {eid}")
    if eid == 2218 and isinstance(d.get("left_open_time"), int) and 2 <= d["left_open_time"] <= 6:
        event = f"Open more than {OPEN_ALARM_MINUTES[d['left_open_time'] - 2]} min"
    source = LOCK_SOURCES.get(d.get("source"), f"source {d.get('source')}") if eid in (2203, 2216) and d.get("source") is not None else None
    return {"time": int(r.get("time") or 0) // 1000, "event": event, "who": str(d.get("source_name") or ""), "source": source}


def _keypad_code(c: dict) -> dict:
    perm, period = c.get("permission") or {}, c.get("period_info") or {}
    # permission.status 3 = one-time, 4 = has an end time (EntryCodeListAdapter.java:112-117); pwd_state 4 = expired.
    kind = {3: "one-time", 4: "temporary"}.get(perm.get("status")) or ("recurring" if period else "permanent")
    return {"name": str(c.get("name") or ""), "type": kind, "expired": c.get("pwd_state") == 4, "begin": perm.get("begin") or 0,
            "end": perm.get("end") or 0, "days": period.get("valid_days") or [], "from": str(period.get("begin") or ""), "to": str(period.get("end") or "")}


def lock_alert(devs: list[dict], now: float | None = None) -> None:
    """Alert once when a lock has been unlocked for lock_minutes; locking it again resets. Off unless lock_minutes is set."""
    now = now or time.time()
    minutes = ALERTS.get("lock_minutes") or 0
    for d in devs:
        if d["type"] != "Lock" or "locked" not in d["state"]:
            continue
        if d["state"]["locked"] or not minutes:
            _unlocked.pop(d["mac"], None)
            _alerted.discard(d["mac"])
        elif now - _unlocked.setdefault(d["mac"], now) >= minutes * 60 and d["mac"] not in _alerted:
            _alerted.add(d["mac"])
            send_webhook("lock", d["name"], f"{d['name']} has been unlocked for {minutes} minutes.", title="Door left unlocked")


def _number_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def record_history(devs: list[dict], now: float | None = None) -> None:
    """Keep thermostat and room-sensor readings from each refresh for the graphs.

    shortcut: the thermostat plugin that calls get_iot_history isn't in the app's APKs (docs/wyze-cloud-history-notes.md), so history starts when the bridge first sees the device.
    """
    now = now or time.time()
    changed = False
    for d in devs:
        s = d["state"]
        if d["model"] not in HISTORY_MODELS or "error" in s:
            continue
        rows = _history.setdefault(d["mac"], [])
        if rows and now - rows[-1]["t"] < HISTORY_STEP:
            continue
        state = str(s.get("working_state") or "").lower()
        row = {"t": int(now), "temp": _number_or_none(s.get("temperature")), "hum": _number_or_none(s.get("humidity")),
               "heat": _number_or_none(s.get("heat_sp")), "cool": _number_or_none(s.get("cool_sp")),
               "run": "heat" if "heat" in state else "cool" if "cool" in state else None}
        rows.append({k: v for k, v in row.items() if v is not None})
        rows[:] = [r for r in rows if r["t"] > now - 7 * 86400]
        changed = True
    if changed:
        try:
            with open(HISTORY_FILE, "w") as f:
                json.dump(_history, f)
        except OSError as ex:
            logger.warning(f"[DEVICES] Couldn't save the device history: {ex}")


def history(mac: str, hours: int, now: float | None = None) -> dict:
    """Readings for the last 24 or 168 hours and minutes of heating and cooling (each reading counts for up to 10 minutes)."""
    now = now or time.time()
    rows = [r for r in _history.get(mac, []) if r["t"] >= now - hours * 3600]
    run = {"heat": 0, "cool": 0}
    for r, nxt in zip(rows, [r["t"] for r in rows[1:]] + [now]):
        if r.get("run"):
            run[r["run"]] += min(nxt - r["t"], 600) // 60
    return {"points": rows, "heat_minutes": int(run["heat"]), "cool_minutes": int(run["cool"])}


def plug_usage(auth: WyzeCredential, dev: dict, fresh: bool = False, now: float | None = None) -> list[dict]:
    """Minutes each hour a plug was on, one row per UTC day for about 9 days (read-only).

    POST v2/plug/usage_record_list, sv from SV_WLPP1_PLUG_USAGE_RECORD_LIST in assets/WLPPO_SV (CloudProtocol.java:2200-2212).
    The window is the app's: tomorrow's local midnight shifted by the zone offset, minus 9 days (duz.java:78-90).
    Replies are {date_ts (ms), data: "[m0,...,m23]"}; the app clamps each hour to 60 (duz.java:164-170).
    """
    mac = dev["parent"] or dev["mac"]  # an outdoor plug's outlets ask with the plug's mac (wlppo/DeviceMainPage.java:329)
    if not fresh and (hit := _usage_cache.get(mac)) and time.time() - hit[0] < LOG_CACHE_SECONDS:
        return hit[1]
    t = time.localtime((now or time.time()) + 86400)
    end = int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)) - t.tm_gmtoff) * 1000
    payload = _payload(auth) | {"sv": "56ed575478344d019f7143971ca6ca86", "phone_system_type": 2, "date_begin": end - 777600000, "date_end": end, "device_mac": mac}
    rows = validate_resp(post(f"{WYZE_API}/v2/plug/usage_record_list", json=payload, headers=_headers())).get("usage_record_list") or []
    out = []
    for r in rows:
        try:
            hours = [min(max(int(m), 0), 60) for m in str(r.get("data")).strip("[] ").split(",")][:24]
            out.append({"date_ts": int(r["date_ts"]), "minutes": hours})
        except (KeyError, TypeError, ValueError):
            continue
    _usage_cache[mac] = (time.time(), sorted(out, key=lambda r: r["date_ts"]))
    return _usage_cache[mac][1]


def start_watch(get_auth, refresh) -> None:
    """Poll every few minutes, only while the lock alert is on or there is history to extend, so the Devices page can be closed."""
    def loop():
        while True:
            time.sleep(WATCH_SECONDS)
            try:
                if (ALERTS.get("lock_minutes") or _history) and get_auth():
                    try:
                        list_devices(get_auth())
                    except AccessTokenError:
                        refresh()
                        list_devices(get_auth())
            except Exception:
                logger.exception("[DEVICES] Background check failed")
    Thread(target=loop, daemon=True, name="devices-watch").start()


def _number(data: dict, key: str, low: int, high: int) -> str:
    try:
        value = int(data[key])
    except (TypeError, ValueError):
        value = low - 1
    if not low <= value <= high:
        raise ValueError(f"{key} must be {low}-{high}")
    return str(value)


def control(auth: WyzeCredential, dev: dict, data: dict) -> str:
    """Apply one command from the page. Returns what was done, for the log. ValueError means bad input."""
    kind, mac, model = dev["type"], dev["mac"], dev["model"]
    if "on" in data and not isinstance(data["on"], bool):
        raise ValueError("on must be true or false")
    if kind in ("OutdoorPlug", "Plug") and "on" in data:
        set_plug(auth, mac, model, bool(data["on"]))
        result = f"turned {'on' if data['on'] else 'off'}"
    elif kind == "MeshLight":
        plist = []
        if "on" in data:
            plist.append({"pid": "P3", "pvalue": "1" if data["on"] else "0"})
        if "brightness" in data:
            plist.append({"pid": "P1501", "pvalue": _number(data, "brightness", 1, 100)})
        if "color_temp" in data:
            plist += [{"pid": "P1508", "pvalue": "2"}, {"pid": "P1502", "pvalue": _number(data, "color_temp", 1800, 6500)}]
        if "color" in data:
            color = str(data["color"]).lstrip("#").upper()
            if len(color) != 6 or any(c not in "0123456789ABCDEF" for c in color):
                raise ValueError("color must be RRGGBB")
            plist += [{"pid": "P1508", "pvalue": "1"}, {"pid": "P1507", "pvalue": color}]
        if not plist:
            raise ValueError("Nothing to change.")
        set_bulb(auth, mac, model, plist)
        result = f"set {plist}"
    elif model == "CO_EA1":
        props = {}
        if "mode" in data:
            if data["mode"] not in MODES:
                raise ValueError(f"mode must be one of {sorted(MODES)}")
            props["mode_sys"] = data["mode"]
        for key in ("heat_sp", "cool_sp"):
            if key in data:
                props[key] = _number(data, key, 4, 95)
        if not props:
            raise ValueError("Nothing to change.")
        set_thermostat(auth, mac, props)
        result = f"set {props}"
    elif kind == "Lock" and data.get("lock") in (True, False):
        set_lock(auth, mac, data["lock"])
        result = "locked" if data["lock"] else "unlocked"
    else:
        raise ValueError("This device can't do that.")
    _cache["at"] = 0.0
    return result
