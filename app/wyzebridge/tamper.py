"""Camera tamper alerts: every few minutes compare a streaming camera's latest snapshot with a learned baseline.

Only reads the snapshot files the bridge already makes (and asks for a fresh one only from cameras that are already
streaming), so it never wakes a camera. A 64x36 grayscale thumbnail is enough for all three checks:
covered (near-uniform image), moved (edge maps no longer line up; edges ignore brightness, and day and night keep
separate baselines) and night vision broken (near-black where the night baseline was IR-lit).
"""

import json
import time
from pathlib import Path
from threading import Lock, Thread
from typing import Callable

# Same limits as detect.py: no wheels on 32-bit ARM, and NumPy refuses very old CPUs.
try:
    import cv2
    import numpy as np
except (ImportError, RuntimeError):
    cv2 = None

from wyzebridge.config import IMG_PATH, TOKEN_PATH
from wyzebridge.logging import logger
from wyzebridge.webhooks import send_webhook

SETTINGS_FILE = TOKEN_PATH + "tamper.json"
SIZE = (64, 36)
CHECK_EVERY = 300
CONFIRM = 3  # checks in a row before alerting: a passing shadow or a reboot doesn't count
REPEAT_AFTER = 6 * 3600  # the same alert on the same camera at most this often
MAX_AGE = 900  # a snapshot older than this says nothing about now
FLAT_STD = 5  # a lens cover is a flat patch: gray-level spread below this
DARK = 15  # average gray level of a picture with no light and no IR
IR_LIT = 30  # a night baseline brighter than this was lit by IR
MOVED_BELOW = 0.5  # edge correlation: the same scene scores 0.9+ whatever the light, a 10% pan about 0.45, a new view under 0.3
MESSAGES = {
    "covered": "The view looks covered or blacked out.",
    "moved": "The camera looks knocked or pointed somewhere else.",
    "night_vision": "Night vision looks broken: the picture is black at night.",
}

_lock = Lock()
STATE: dict[str, str] = {}  # uri -> what looks wrong right now, for the Health page
_streak: dict[str, tuple[str, int]] = {}
_alerted: dict[tuple[str, str], float] = {}


def _load() -> dict:
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _save(data: dict) -> None:
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f)


def enabled() -> bool:
    return bool(cv2) and bool(_load().get("on"))  # off until the owner turns it on: untested on real footage


def set_enabled(value) -> bool:
    if not isinstance(value, bool):
        raise ValueError("Use true or false")
    if value and not cv2:
        raise ValueError("Tamper alerts need OpenCV, which isn't available on this device")
    with _lock:
        _save(_load() | {"on": value})
        if not value:
            STATE.clear()
            _streak.clear()
    return value


def reset(uri: str) -> None:
    """The scene is the new normal: forget the baselines and learn again from the next snapshot."""
    with _lock:
        data = _load()
        data.get("base", {}).pop(uri, None)
        _save(data)
        STATE.pop(uri, None)
        _streak.pop(uri, None)
        for problem in MESSAGES:
            _alerted.pop((uri, problem), None)


def _edges(gray):
    blur = cv2.GaussianBlur(gray.astype("float32"), (0, 0), 1.5)
    mag = cv2.magnitude(cv2.Sobel(blur, cv2.CV_32F, 1, 0), cv2.Sobel(blur, cv2.CV_32F, 0, 1))
    return cv2.GaussianBlur(mag, (0, 0), 1.5)  # so wind or a pixel of nudge still matches


def similarity(a, b) -> float:
    """Correlation of two thumbnails' edge maps, -1 to 1. Brightness and contrast changes don't move it."""
    ea, eb = _edges(a).ravel(), _edges(b).ravel()
    ea, eb = ea - ea.mean(), eb - eb.mean()
    denom = float(np.linalg.norm(ea) * np.linalg.norm(eb))
    return float(ea @ eb) / denom if denom else 0.0


def classify(thumb, color: bool, base: dict) -> str:
    """What's wrong with a thumbnail, or "". Learns that period's baseline (day or night) the first time."""
    mean, std = float(thumb.mean()), float(thumb.std())
    period = "day" if color and mean >= DARK else "night"  # IR mode is gray; a dark color picture is lights off
    saved = base.get(period)
    if period == "night" and mean < DARK and saved and saved["mean"] >= IR_LIT:
        return "night_vision"
    if std < FLAT_STD:
        return "covered" if mean >= DARK else ""  # flat and dark with no IR history is just a dark room
    if not saved:
        base[period] = {"mean": mean, "img": thumb.tolist()}
        return ""
    return "moved" if similarity(np.array(saved["img"], dtype="uint8"), thumb) < MOVED_BELOW else ""


def observe(uri: str, path: str | Path, now: float | None = None) -> str:
    """Judge one snapshot. Alerts after CONFIRM bad checks in a row. Returns the current problem, or ""."""
    now = now or time.time()
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        return ""
    small = cv2.resize(img, SIZE, interpolation=cv2.INTER_AREA)
    color = float(np.abs(small[..., 0].astype(int) - small[..., 2]).mean()) > 3
    thumb = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    with _lock:
        data = _load()
        base = data.setdefault("base", {}).setdefault(uri, {})
        learned = len(base)
        problem = classify(thumb, color, base)
        if len(base) != learned:
            _save(data)
        last, count = _streak.get(uri, ("", 0))
        count = count + 1 if problem and problem == last else 1 if problem else 0
        _streak[uri] = (problem, count)
        if count >= CONFIRM:
            STATE[uri] = problem
        else:
            STATE.pop(uri, None)
        if count < CONFIRM or now - _alerted.get((uri, problem), -REPEAT_AFTER) < REPEAT_AFTER:
            return problem
        _alerted[(uri, problem)] = now
    send_webhook("tamper", uri, f"{uri}: {MESSAGES[problem]}", title="Camera tampering?")
    return problem


def start(snap: Callable[[str], bool], streaming: Callable[[], list[str]]) -> None:
    """snap(uri) refreshes IMG_PATH/<uri>.jpg and says whether it worked; streaming() lists cameras already streaming."""

    def loop():
        while True:
            time.sleep(CHECK_EVERY)
            try:
                if not enabled():
                    continue
                for uri in streaming():
                    path = Path(IMG_PATH, f"{uri}.jpg")
                    if snap(uri) and path.exists() and time.time() - path.stat().st_mtime < MAX_AGE:
                        observe(uri, path)
            except Exception as ex:
                logger.warning(f"[TAMPER] {ex}")

    if cv2:
        Thread(target=loop, name="tamper", daemon=True).start()
