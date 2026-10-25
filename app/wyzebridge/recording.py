"""Which cameras record, picked on the Recordings page instead of RECORD_* env vars, and how much disk that takes."""

import json
import os
import shutil
import time
from pathlib import Path
from threading import Thread

from wyzebridge.config import TOKEN_PATH
from wyzebridge.logging import logger
from wyzebridge.mtx_server import RECORD_PATH

SETTINGS_FILE = TOKEN_PATH + "record.json"
KEEP_CHOICES = (1, 3, 7, 14, 30)
MAX_GB_CHOICES = (0, 25, 50, 100, 250, 500, 1000)  # 0 is no limit
MIN_FREE = 5 * 1024**3  # always leave this much of the drive free, whatever the settings
DEFAULT_KEEP_DAYS = 7  # a camera writes roughly 10-20 GB a day, so a fresh install can't quietly fill the disk


def load_settings() -> dict:
    """{"all": bool, "cams": [uri, ...], "keep_days": int} saved from the web UI."""
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_settings(form: dict, uris: list[str]) -> dict:
    """Validate and save. Raises ValueError with a message for the user."""
    cams = form.get("cams") or []
    if not isinstance(cams, list) or not all(isinstance(c, str) for c in cams):
        raise ValueError("Pick cameras from the list")
    try:
        keep = int(form.get("keep_days") or DEFAULT_KEEP_DAYS)
    except (TypeError, ValueError):
        keep = 0
    if keep not in KEEP_CHOICES:
        raise ValueError("Pick how long to keep recordings from the list")
    try:
        max_gb = int(form.get("max_gb") or 0)
    except (TypeError, ValueError):
        max_gb = -1
    if max_gb not in MAX_GB_CHOICES:
        raise ValueError("Pick a space limit from the list")
    settings = {"all": bool(form.get("all")), "cams": [c for c in cams if c in uris], "keep_days": keep, "max_gb": max_gb}
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f)
    return settings


def wanted(uri: str) -> bool:
    settings = load_settings()
    return bool(settings.get("all")) or uri in settings.get("cams", [])


def keep() -> str:
    """For MediaMTX's recordDeleteAfter."""
    return f"{(load_settings().get('keep_days') or DEFAULT_KEEP_DAYS) * 86400}s"


def record_root() -> Path:
    """The folder above every camera's recordings: RECORD_PATH up to its first placeholder."""
    return Path(RECORD_PATH.split("%")[0] + "x").parent


def disk_use() -> dict:
    """Bytes used by recordings and free on that drive. Free is 0 if the drive can't be read."""
    root = record_root()
    # shortcut: walks every clip on each page load (~10k files a camera a week); cache it if the page gets slow.
    used = sum(_size(f) for f in root.rglob("*.mp4"))
    drive = next((p for p in (root, *root.parents) if p.exists()), Path(os.getcwd()))
    try:
        free = shutil.disk_usage(drive).free
    except OSError:
        free = 0
    return {"used": used, "free": free}


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:  # MediaMTX deleted an old clip while we counted
        return 0


def trim() -> int:
    """Delete the oldest clips while recordings are over the space limit or the drive is nearly full. Returns bytes freed."""
    root = record_root()
    # Clips still being written (touched in the last 2 minutes) are left alone.
    clips = sorted((f for f in root.rglob("*.mp4") if _mtime(f) < time.time() - 120), key=_mtime)
    use = disk_use()
    limit = (load_settings().get("max_gb") or 0) * 1024**3
    over = max(use["used"] - limit if limit else 0, MIN_FREE - use["free"] if use["free"] else 0)
    freed = 0
    for clip in clips:
        if freed >= over:
            break
        size = _size(clip)
        try:
            clip.unlink()
        except OSError:
            continue
        freed += size
    if freed:
        logger.info(f"[RECORD] Deleted {freed // 1024**2} MB of the oldest clips to stay under the space limit")
    return freed


def start_trimming() -> None:
    def loop():
        while True:
            try:
                trim()
            except OSError as ex:
                logger.warning(f"[RECORD] Couldn't clean up old clips: {ex}")
            time.sleep(300)

    Thread(target=loop, name="record-trim", daemon=True).start()


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return time.time()
