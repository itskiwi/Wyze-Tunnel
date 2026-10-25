"""Which cameras record, picked on the Recordings page instead of RECORD_* env vars, and how much disk that takes."""

import contextlib
import json
import os
import shutil
import sys
import time
from datetime import date
from pathlib import Path
from threading import Lock, Thread

from wyzebridge.config import IMG_PATH, TOKEN_PATH
from wyzebridge.logging import logger
from wyzebridge.mtx_server import RECORD_PATH

SETTINGS_FILE = TOKEN_PATH + "record.json"
CLOUD_FILE = TOKEN_PATH + "clip_backup.json"
CLOUD_DIR = "Wyze Tunnel"  # copies go only under <folder>/Wyze Tunnel, and pruning looks nowhere else
_cloud_lock = Lock()  # the trim loop and a fresh save from the page must not copy the same clip at once
KEEP_CHOICES = (1, 3, 7, 14, 30)
MAX_GB_CHOICES = (0, 25, 50, 100, 250, 500, 1000)  # 0 is no limit
MIN_FREE = 5 * 1024**3  # always leave this much of the drive free, whatever the settings
MIN_FREE_SHARE = 0.15  # or this share of the drive, if more: Windows slows down when nearly full
MAX_FREE_FLOOR = 20 * 1024**3  # but never reserve more than this: 15% of a 2 TB drive would wipe every clip
KEEP_RECENT = 86400  # the free-space floor never takes the last day of clips, unless the drive is under MIN_FREE
DRIVE_FULL = False  # set by trim() when other files fill the drive and deleting old clips can't make room
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
    """Bytes used by recordings, free and total on that drive. Free is 0 if the drive can't be read."""
    root = record_root()
    # shortcut: walks every clip on each page load (~10k files a camera a week); cache it if the page gets slow.
    used = sum(_size(f) for f in root.rglob("*.mp4"))
    drive = next((p for p in (root, *root.parents) if p.exists()), Path(os.getcwd()))
    try:
        usage = shutil.disk_usage(drive)
        free, total = usage.free, usage.total
    except OSError:
        free = total = 0
    return {"used": used, "free": free, "total": total}


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
    floor = max(MIN_FREE, min(use.get("total", 0) * MIN_FREE_SHARE, MAX_FREE_FLOOR))
    # The space limit and the 5 GB minimum may take any clip; the softer floor stops at the last day.
    must = max(use["used"] - limit if limit else 0, MIN_FREE - use["free"] if use["free"] else 0)
    over = max(must, floor - use["free"] if use["free"] else 0)
    recent = time.time() - KEEP_RECENT
    freed = 0
    for clip in clips:
        if freed >= over or (freed >= must and _mtime(clip) > recent):
            break
        size = _size(clip)
        try:
            clip.unlink()
        except OSError:
            continue
        freed += size
    # Clip-list pictures outlive their clips otherwise; recordings are kept 30 days at most.
    for thumb in Path(IMG_PATH, "clip-thumbs").rglob("*.jpg"):
        if _mtime(thumb) < time.time() - 31 * 86400:
            thumb.unlink(missing_ok=True)
    global DRIVE_FULL
    DRIVE_FULL = freed < over
    if freed:
        logger.info(f"[RECORD] Deleted {freed // 1024**2} MB of the oldest clips to stay under the space limit")
    return freed


def load_cloud() -> dict:
    """{"folder": str, "cams": [uri, ...] (empty is every camera), "last": time of the last good run, "error": str}."""
    try:
        with open(CLOUD_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _write_cloud(settings: dict) -> None:
    with open(CLOUD_FILE, "w") as f:
        json.dump(settings, f)


def save_cloud(form: dict, uris: list[str]) -> dict:
    """Turn copying clips to a folder on (a folder) or off (an empty folder). Raises ValueError with a message for the user."""
    from wyzebridge import backup  # backup imports this module

    cams = form.get("cams") or []
    if not isinstance(cams, list) or not all(isinstance(c, str) for c in cams):
        raise ValueError("Pick cameras from the list.")
    settings = {}
    if folder := str(form.get("folder") or "").strip():
        backup.check_folder(folder)
        settings = {"folder": folder, "cams": [c for c in cams if c in uris], "last": 0, "error": ""}
    _write_cloud(settings)
    return settings


def back_up_clips(now: float | None = None, budget: float = 240) -> int:
    """Copy finished clips into the chosen folder, then delete copies older than the keep time. Returns clips copied."""
    if not _cloud_lock.acquire(blocking=False):
        return 0  # a run is already going
    try:
        return _back_up_clips(now, budget)
    finally:
        _cloud_lock.release()


def _back_up_clips(now: float | None, budget: float) -> int:
    settings = load_cloud()
    if not (folder := settings.get("folder")):
        return 0
    now = time.time() if now is None else now
    root, dest = record_root(), Path(folder, CLOUD_DIR)
    cutoff = now - (load_settings().get("keep_days") or DEFAULT_KEEP_DAYS) * 86400
    cams, copied, error = settings.get("cams") or [], 0, ""
    started = time.time()
    try:
        if not Path(folder).is_dir():  # an unplugged drive or a removed cloud app; don't recreate it on the wrong drive
            raise FileNotFoundError(2, "the folder isn't there any more")
        for clip in sorted(root.rglob("*.mp4"), key=_mtime):
            if time.time() - started > budget:  # the rest go on the next run, so trimming isn't held up
                break
            mtime, rel = _mtime(clip), clip.relative_to(root)
            # shortcut: takes the camera from the first folder under the record root, as the default RECORD_PATH has it.
            cam = rel.parts[0] if len(rel.parts) > 1 else "other"
            # Clips still being written, and ones about to be deleted anyway, are skipped.
            if not cutoff < mtime < now - 120 or (cams and cam not in cams):
                continue
            target = dest / cam / date.fromtimestamp(mtime).isoformat() / clip.name
            if target.exists() and target.stat().st_size == _size(clip):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            part = target.with_name(target.name + ".part")  # a half-copied clip never looks finished
            try:
                shutil.copy2(clip, part)
            except FileNotFoundError:
                if clip.exists():
                    raise
                continue  # deleted by MediaMTX while we copied
            part.replace(target)
            copied += 1
        for old in dest.glob("*/*/*.mp4"):
            if _mtime(old) < cutoff:
                old.unlink(missing_ok=True)
        for day in dest.glob("*/*"):
            with contextlib.suppress(OSError):
                day.rmdir()  # only empty day folders go
    except OSError as ex:
        error = f"Couldn't copy clips to {folder}: {ex.strerror or ex}. Trying again in 5 minutes."
        if error != settings.get("error"):
            logger.warning(f"[RECORD] {error}")
    if copied:
        logger.info(f"[RECORD] Backed up {copied} clip{'s'[:copied ^ 1]} to {dest}")
    if (current := load_cloud()).get("folder") == folder:  # not turned off or moved while we copied
        _write_cloud({**current, "last": current.get("last", 0) if error else now, "error": error})
    return copied


def start_trimming() -> None:
    def loop():
        while True:
            try:
                trim()
            except OSError as ex:
                logger.warning(f"[RECORD] Couldn't clean up old clips: {ex}")
            back_up_clips()
            time.sleep(300)

    Thread(target=loop, name="record-trim", daemon=True).start()


_awake = False


def keep_awake(on: bool) -> None:
    """Stop Windows sleeping while cameras record. Call from one long-lived thread: the flag is per thread."""
    global _awake
    if on == _awake or sys.platform != "win32":
        return
    import ctypes

    # ES_CONTINUOUS, plus ES_SYSTEM_REQUIRED to hold the PC awake
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if on else 0))
    _awake = on


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return time.time()
