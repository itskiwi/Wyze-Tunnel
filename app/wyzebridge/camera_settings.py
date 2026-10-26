"""Camera settings at a glance: read common settings, spot drift, apply to many, snapshot and restore.

Everything goes through the existing send_cmd (GET_CMDS / SET_CMDS); no new camera commands. Only cameras that are
already streaming are asked, so idle and battery cameras are never woken just to read a setting."""
import json
import time
from collections import Counter
from datetime import datetime, timezone
from threading import Lock

from wyzebridge.config import TOKEN_PATH

SNAPSHOT_FILE = TOKEN_PATH + "setting_snapshots.json"
MAX_SNAPSHOTS = 20
STALE_SECS = 600
PAUSE = 0.3  # between commands so a camera isn't hammered

# What the grid can read (column order). Flips are shown raw: which of 1/2 means "flipped" isn't verified.
COLUMNS = {
    "night_vision": "Night vision",
    "irled": "IR lights",
    "status_light": "Status light",
    "motion_detection": "Motion detection",
    "notifications": "Notifications",
    "osd_timestamp": "Timestamp",
    "hor_flip": "Flip (horizontal)",
    "ver_flip": "Flip (vertical)",
    "bitrate": "Bitrate",
    "quality": "Stream quality",
}
# The only settings the grid may write, and their allowed values. No SD format, reboot, power, Wi-Fi, firmware,
# resolution, flips or anything that moves the camera.
WRITABLE = {
    "night_vision": ("on", "off", "auto"),
    "irled": ("on", "off"),
    "status_light": ("on", "off"),
    "motion_detection": ("on", "off"),
    "notifications": ("on", "off"),
    "osd_timestamp": ("on", "off"),
    "bitrate": ("30", "60", "120", "180", "240"),
    "quality": ("hd", "sd"),
}
ONOFF = {"1": "on", "on": "on", "true": "on", "2": "off", "off": "off", "false": "off", "3": "auto", "auto": "auto"}

CACHE: dict[str, dict] = {}  # uri: {"ts": float, "values": {setting: value}}
LOCK = Lock()  # one command run at a time: cameras share a single command queue


def normalize(key: str, value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None  # e.g. a bitrate answer that came back as a dict
    text = str(value).strip().lower()
    return text if key in {"bitrate", "quality", "hor_flip", "ver_flip"} else ONOFF.get(text, text)


def targets(streams: dict) -> dict:
    return {u: s for u, s in streams.items() if not s.options.substream}


def read_cam(uri: str, stream, send, sleep=time.sleep) -> dict:
    values = {}
    for key in COLUMNS:
        if not stream.connected:
            break
        resp = send(uri, key, "")
        if resp.get("status") == "success" and (val := normalize(key, resp.get("value"))) is not None:
            values[key] = val
        sleep(PAUSE)
    CACHE[uri] = {"ts": time.time(), "values": values}
    return values


def refresh(streams: dict, send, sleep=time.sleep) -> bool:
    """Read every streaming camera, one at a time. False if another run is going."""
    if not LOCK.acquire(blocking=False):
        return False
    try:
        for uri, stream in targets(streams).items():
            if stream.connected:
                read_cam(uri, stream, send, sleep)
    finally:
        LOCK.release()
    return True


def drift(cache: dict, streams: dict) -> dict[str, list[str]]:
    """Per setting, the cameras whose value differs from the majority. No clear majority means no drift."""
    out = {}
    for key in COLUMNS:
        vals = {u: c["values"][key] for u, c in cache.items() if u in streams and key in c["values"]}
        top = Counter(vals.values()).most_common(2)
        if len(vals) < 2 or len(top) < 2 or top[0][1] == top[1][1]:
            continue
        if odd := sorted(u for u, v in vals.items() if v != top[0][0]):
            out[key] = odd
    return out


def table(streams: dict) -> dict:
    now = time.time()
    cams = []
    for uri, s in targets(streams).items():
        entry = CACHE.get(uri)
        cams.append({"uri": uri, "name": s.camera.nickname, "connected": s.connected,
                     "values": entry["values"] if entry else {},
                     "age": int(now - entry["ts"]) if entry else None})
    return {"columns": COLUMNS, "writable": {k: list(v) for k, v in WRITABLE.items()}, "cams": cams,
            "drift": drift(CACHE, targets(streams)), "busy": LOCK.locked(), "stale": STALE_SECS}


def _send_one(send, uri: str, key: str, value: str) -> dict:
    resp = send(uri, key, value) or {}
    if resp.get("status") == "success":
        return {"result": "ok"}
    text = str(resp.get("response") or "").lower()
    kind = "not supported" if any(t in text for t in ("invalid command", "not support", "only the siren")) else "failed"
    return {"result": kind, "detail": text[:120]}


def apply(streams: dict, send, key: str, value: str, uris, sleep=time.sleep) -> list[dict]:
    """Set one allowlisted setting on the chosen cameras. Raises ValueError for anything not allowed."""
    if key not in WRITABLE:
        raise ValueError("That setting can't be changed here.")
    if value not in WRITABLE[key]:
        raise ValueError(f"Use one of: {', '.join(WRITABLE[key])}.")
    pool = targets(streams)
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and u in pool for u in uris):
        raise ValueError("Pick at least one camera.")
    if not LOCK.acquire(blocking=False):
        raise RuntimeError("busy")
    try:
        return [_apply_cam(uri, pool[uri], send, {key: value}, sleep) for uri in dict.fromkeys(uris)]
    finally:
        LOCK.release()


def _apply_cam(uri: str, stream, send, changes: dict, sleep) -> dict:
    row = {"uri": uri, "name": stream.camera.nickname, "changes": {}}
    if not stream.connected:  # never wake a camera to change it
        return row | {"result": "not connected"}
    for key, value in changes.items():
        res = _send_one(send, uri, key, value)
        row["changes"][key] = res
        if res["result"] == "ok" and uri in CACHE:
            CACHE[uri]["values"][key] = value
        sleep(PAUSE)
    results = {c["result"] for c in row["changes"].values()}
    row["result"] = "ok" if results <= {"ok"} else ("not supported" if results == {"not supported"} else "failed")
    if len(results) == 1:
        row["detail"] = next(iter(row["changes"].values())).get("detail", "")
    return row


def _load() -> list[dict]:
    try:
        with open(SNAPSHOT_FILE) as f:
            snaps = json.load(f)
        return snaps if isinstance(snaps, list) else []
    except (FileNotFoundError, ValueError):
        return []


def save_snapshot(streams: dict) -> dict:
    """Keep the last readings of every streaming camera. Raises ValueError if nothing has been read yet."""
    live = targets(streams)
    cams = {u: {"name": live[u].camera.nickname, "values": dict(c["values"])}
            for u, c in CACHE.items() if u in live and live[u].connected and c["values"]}
    if not cams:
        raise ValueError("Read the cameras first, then save.")
    ts = datetime.now(timezone.utc)
    snap = {"id": ts.strftime("%Y%m%dT%H%M%S%fZ"), "ts": ts.isoformat(timespec="seconds"), "cams": cams}
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump((_load() + [snap])[-MAX_SNAPSHOTS:], f)
    return {"id": snap["id"], "ts": snap["ts"], "cams": len(cams)}


def list_snapshots() -> list[dict]:
    return [{"id": s["id"], "ts": s["ts"], "cams": {u: c["name"] for u, c in s["cams"].items()}} for s in reversed(_load())]


def restore_diff(snap_id: str, uri: str, current: dict) -> dict[str, dict]:
    """What restoring would change: only allowlisted settings whose saved value differs from the camera's now."""
    snap = next((s for s in _load() if s["id"] == snap_id), None)
    if not snap or uri not in snap["cams"]:
        raise ValueError("That snapshot has no settings for this camera.")
    saved = snap["cams"][uri]["values"]
    return {k: {"saved": v, "current": current.get(k)} for k, v in saved.items()
            if k in WRITABLE and v in WRITABLE[k] and current.get(k) != v}


def restore(streams: dict, send, snap_id: str, uri: str, confirm: bool, sleep=time.sleep) -> dict:
    """Preview (confirm false) or apply one snapshot to one connected camera. Reads it fresh first."""
    stream = targets(streams).get(uri)
    if not stream:
        raise ValueError("Unknown camera.")
    if not stream.connected:
        raise ValueError("The camera isn't streaming right now, so its settings can't be read or changed.")
    if not LOCK.acquire(blocking=False):
        raise RuntimeError("busy")
    try:
        diff = restore_diff(snap_id, uri, read_cam(uri, stream, send, sleep))
        if not confirm:
            return {"diff": diff}
        row = _apply_cam(uri, stream, send, {k: d["saved"] for k, d in diff.items()}, sleep) if diff else {"result": "ok", "changes": {}}
        return {"diff": diff, "applied": row}
    finally:
        LOCK.release()
