"""Which cameras record, picked on the Recordings page instead of RECORD_* env vars, and how much disk that takes."""

import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from bisect import bisect_left
from datetime import date, timedelta
from functools import cache
from pathlib import Path
from threading import Lock, Thread

from wyzebridge.bridge_utils import env_cam
from wyzebridge.config import IMG_PATH, TOKEN_PATH
from wyzebridge.ffmpeg import parse_timedelta
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
DEFAULT_KEEP_DAYS = 7  # a camera writes roughly 10-20 GB a day, so a fresh install can't quietly fill the disk
# After keep_days, only clips near motion or a detection stay, up to this long (Frigate's "retain: motion" idea).
EVENT_CHOICES = (0, 14, 30, 60, 90)  # 0 is off: everything goes at keep_days
DEFAULT_EVENT_DAYS = 30  # most of a camera's day is an empty yard, so a month of moments takes about a week's space
MOMENTS_FILE = TOKEN_PATH + "moments.db"
PAD = 30  # seconds kept either side of a moment, so the walk up to the door stays too
# MediaMTX cuts at the next keyframe after this, so a clip can run a few seconds longer; PAD covers that.
SEGMENT_SECONDS = (parse_timedelta("RECORD_LENGTH") or timedelta(seconds=60)).total_seconds()
_moments_lock = Lock()


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
    try:
        event_days = int(DEFAULT_EVENT_DAYS if form.get("event_days") is None else form["event_days"])
    except (TypeError, ValueError):
        event_days = -1
    if event_days not in EVENT_CHOICES:
        raise ValueError("Pick how long to keep moments from the list")
    settings = {"all": bool(form.get("all")), "cams": [c for c in cams if c in uris], "keep_days": keep, "max_gb": max_gb,
                "event_days": event_days, "shrink": bool(form.get("shrink")) and bool(shrink_encoder())}
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f)
    return settings


def wanted(uri: str) -> bool:
    settings = load_settings()
    return bool(settings.get("all")) or uri in settings.get("cams", [])


def keep() -> str:
    """For MediaMTX's recordDeleteAfter: never, because expire() deletes them, sparing moments and starred clips."""
    return "0s"


def keep_days(settings: dict | None = None) -> tuple[int, int]:
    """(days every clip is kept, days clips with a moment are kept); the second is never shorter."""
    settings = load_settings() if settings is None else settings
    days = settings.get("keep_days") or DEFAULT_KEEP_DAYS
    return days, max(days, settings.get("event_days", DEFAULT_EVENT_DAYS))


def _moments_db() -> sqlite3.Connection:
    db = sqlite3.connect(MOMENTS_FILE, timeout=10)
    db.execute("CREATE TABLE IF NOT EXISTS moments (uri TEXT, ts REAL, star INTEGER DEFAULT 0)")
    db.execute("CREATE INDEX IF NOT EXISTS moments_ts ON moments (ts)")
    db.execute("CREATE TABLE IF NOT EXISTS shrunk (path TEXT PRIMARY KEY)")
    return db


def mark(uri: str, ts: float) -> None:
    """Motion or a detection on this camera at ts: the clips around it outlive keep_days."""
    with contextlib.suppress(sqlite3.Error), _moments_lock, _moments_db() as db:
        db.execute("INSERT INTO moments (uri, ts) VALUES (?, ?)", (uri, ts))


def star(uri: str, ts: float, on: bool = True) -> None:
    """A starred event: the clips around it are never deleted on their own, by age or for space."""
    with _moments_lock, _moments_db() as db:
        db.execute("DELETE FROM moments WHERE uri = ? AND ts = ? AND star = 1", (uri, ts))
        if on:
            db.execute("INSERT INTO moments (uri, ts, star) VALUES (?, ?, 1)", (uri, ts))


def _moments(since: float) -> dict[str, tuple[list[float], list[float]]]:
    """{uri: (sorted moment times since `since`, sorted starred times)}, and drops moments too old to matter."""
    out: dict[str, tuple[list[float], list[float]]] = {}
    try:
        with _moments_lock, _moments_db() as db:
            db.execute("DELETE FROM moments WHERE star = 0 AND ts < ?", (since,))
            rows = db.execute("SELECT uri, ts, star FROM moments WHERE ts >= ? OR star = 1 ORDER BY ts", (since,)).fetchall()
    except sqlite3.Error as ex:
        # Without the list every old clip would look empty, so expire() and trim() must not trust it.
        raise OSError(f"can't read the moments list: {ex}") from ex
    for uri, ts, starred in rows:
        out.setdefault(uri, ([], []))[1 if starred else 0].append(ts)
    return out


def _near(times: list[float], mtime: float) -> bool:
    """A time in the clip ending at mtime, or within PAD of it."""
    i = bisect_left(times, mtime - SEGMENT_SECONDS - PAD)
    return i < len(times) and times[i] <= mtime + PAD


def _kind(clip: Path, root: Path, mtime: float, moments: dict) -> str:
    """"star", "moment" or "" for a plain clip."""
    rel = clip.relative_to(root)
    # shortcut: takes the camera from the first folder under the record root, as the default RECORD_PATH has it.
    times, stars = moments.get(rel.parts[0] if len(rel.parts) > 1 else "", ([], []))
    return "star" if _near(stars, mtime) else "moment" if _near(times, mtime) else ""


def expire(now: float | None = None, budget: float = 120) -> int:
    """Delete clips past keep_days unless a moment is in them, and moments past event_days. Starred clips stay.
    Kept moments are shrunk with the graphics chip if that's on. Returns clips deleted."""
    now = time.time() if now is None else now
    settings = load_settings()
    days, event_days = keep_days(settings)
    root, deleted, started = record_root(), 0, time.time()
    moments = _moments(now - event_days * 86400 - 86400)
    ours = set(settings.get("cams") or [])
    for clip in root.rglob("*.mp4"):
        mtime, rel = _mtime(clip), clip.relative_to(root)
        if mtime > now - days * 86400 or len(rel.parts) < 2:
            continue
        # Cameras recording for the config (RECORD_KEEP) or only for Search footage keep MediaMTX's own keep time.
        # shortcut: their starred clips aren't protected from that; move them here if people star those.
        if not (settings.get("all") or rel.parts[0] in ours) or env_cam("record", rel.parts[0]):
            continue
        kind = _kind(clip, root, mtime, moments)
        if kind == "star" or (kind == "moment" and mtime > now - event_days * 86400):
            if settings.get("shrink") and time.time() - started < budget:
                shrink(clip)
            continue
        try:
            clip.unlink()
            deleted += 1
        except OSError:
            continue
    with _moments_lock, _moments_db() as db:  # forget clips that are gone
        db.executemany("DELETE FROM shrunk WHERE path = ?", [r for r in db.execute("SELECT path FROM shrunk") if not Path(r[0]).exists()])
    if deleted:
        logger.info(f"[RECORD] Deleted {deleted} old clip{'s'[:deleted ^ 1]} with nothing happening in them")
    return deleted


@cache
def shrink_encoder() -> str:
    """The first hardware H.264 encoder this PC's ffmpeg can use, or "" if none. Software is too slow to run all day."""
    for enc in ("h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox"):
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=s=320x240:d=0.2", "-c:v", enc, "-f", "null", "-"]
        try:
            if subprocess.run(cmd, capture_output=True, timeout=20).returncode == 0:
                return enc
        except (OSError, subprocess.TimeoutExpired):
            return ""  # no ffmpeg at all
    return ""


def shrink(clip: Path) -> bool:
    """Re-encode a kept clip at a lower bit rate, once. The original stays unless the copy plays and is much smaller."""
    with _moments_lock, _moments_db() as db:
        if db.execute("INSERT OR IGNORE INTO shrunk VALUES (?)", (str(clip),)).rowcount == 0:
            return False  # tried before, whatever came of it
    if not (enc := shrink_encoder()):
        return False
    part = clip.with_name(clip.name + ".part")
    # Fragmented like MediaMTX writes them, so the Recordings page can still play it; audio is copied as it is.
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip), "-map", "0", "-c:v", enc,
           "-b:v", "700k", "-maxrate", "1000k", "-bufsize", "2000k", "-c:a", "copy",
           "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", str(part)]
    try:
        stat = clip.stat()
        ok = subprocess.run(cmd, capture_output=True, timeout=300).returncode == 0
        if ok and 0 < part.stat().st_size < stat.st_size * 0.8:
            os.utime(part, (stat.st_atime, stat.st_mtime))  # its age decides when it goes
            part.replace(clip)
            return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    part.unlink(missing_ok=True)
    return False


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
    root, now = record_root(), time.time()
    moments = _moments(now - keep_days()[1] * 86400 - 86400)

    def order(clip: Path) -> tuple:
        # Empty footage goes first, then moments, then the last day; oldest first within each.
        mtime = _mtime(clip)
        return 2 if mtime > now - 86400 else 1 if _kind(clip, root, mtime, moments) else 0, mtime

    # Clips still being written (touched in the last 2 minutes) and starred ones are left alone.
    clips = sorted((f for f in root.rglob("*.mp4") if _mtime(f) < now - 120 and _kind(f, root, _mtime(f), moments) != "star"), key=order)
    use = disk_use()
    limit = (load_settings().get("max_gb") or 0) * 1024**3
    floor = max(MIN_FREE, min(use.get("total", 0) * MIN_FREE_SHARE, MAX_FREE_FLOOR))
    over = max(use["used"] - limit if limit else 0, floor - use["free"] if use["free"] else 0)
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
    # Clip-list pictures outlive their clips otherwise; recordings are kept 30 days at most.
    for thumb in Path(IMG_PATH, "clip-thumbs").rglob("*.jpg"):
        if _mtime(thumb) < time.time() - 31 * 86400:
            thumb.unlink(missing_ok=True)
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
                expire()
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
