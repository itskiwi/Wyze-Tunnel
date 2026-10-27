"""Keeps every camera event's picture and clip (Wyze deletes its own after 14 days), plus sensor history, for the History page."""

import hashlib
import os
import re
import sqlite3
import time
from pathlib import Path
from threading import Lock

from wyzebridge.config import EVENT_ARCHIVE_GB, TOKEN_PATH
from wyzebridge.logging import logger

ARCHIVE_DIR = Path(TOKEN_PATH, "event_archive")  # kept by "Start over"
MAX_IMG = 10 * 1024**2
MAX_CLIP = 150 * 1024**2
DOWNLOAD_SECONDS = 120
ID_RE = re.compile(r"[0-9a-f]{32}")
_lock = Lock()  # one writer: the save threads and the prune must not interleave
_last_poll = 0.0
SENSOR_POLL = 300
# Wyze sensors report "1" for open or motion; the Wyze app's History page reads it the same way.
SENSOR_TEXT = {"DWS": {"1": "opened", "0": "closed"}, "PIR": {"1": "motion", "0": "motion stopped"}}


def _db() -> sqlite3.Connection:
    (ARCHIVE_DIR / "media").mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(ARCHIVE_DIR / "index.db", timeout=10)
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, ts INTEGER, device TEXT, type TEXT, text TEXT,"
        " img TEXT, clip TEXT, size INTEGER)"
    )
    return db


def make_id(*parts: str) -> str:
    """A fixed-length hex id, so Wyze's event ids never reach a file name or leak a MAC."""
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def _download(api, url: str | None, dest: Path, max_bytes: int) -> int:
    """Save url to dest. Returns bytes written, or 0 if it failed or was too big (nothing is left behind)."""
    if not url or not url.startswith(("http://", "https://")):
        return 0
    part = dest.with_suffix(dest.suffix + ".part")
    start, size = time.time(), 0
    try:
        with api.get_media(url, timeout=(5, 20), stream=True) as resp:
            resp.raise_for_status()
            if int(resp.headers.get("Content-Length") or 0) > max_bytes:
                return 0
            with open(part, "wb") as f:
                for chunk in resp.iter_content(65536):
                    size += len(chunk)
                    if size > max_bytes or time.time() - start > DOWNLOAD_SECONDS:
                        raise ValueError("too big or too slow")
                    f.write(chunk)
        os.replace(part, dest)
        return size
    except Exception as ex:
        logger.warning(f"[ARCHIVE] Couldn't save an event {dest.suffix} file: {type(ex).__name__}")
        part.unlink(missing_ok=True)
        return 0


def save_event(api, event_id: str, entry: dict, img_url: str | None, clip_url: str | None) -> bool:
    """Save a camera event and its files. entry is the EVENT_LOG dict. False if already saved."""
    eid = make_id("camera", str(event_id))
    with _db() as db:
        if db.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone():
            return False
    media = ARCHIVE_DIR / "media"
    img = f"{eid}.jpg" if (n1 := _download(api, img_url, media / f"{eid}.jpg", MAX_IMG)) else None
    clip = f"{eid}.mp4" if (n2 := _download(api, clip_url, media / f"{eid}.mp4", MAX_CLIP)) else None
    row = (eid, int(entry["ts"]), entry["name"], entry.get("label") or "motion", entry["text"], img, clip, n1 + n2)
    with _lock, _db() as db:
        db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?)", row)
    prune()
    return True


def save_sensor_events(events: list[dict]) -> int:
    """Store sensor history rows from WyzeApi.get_sensor_events. Returns how many are new."""
    rows = []
    for ev in events:
        try:
            kind = str(ev["device_model"])[:3]
            text = SENSOR_TEXT[kind][str(ev["event_value"])]
            rows.append((make_id("sensor", ev["device_mac"], str(ev["event_id"])), int(ev["event_ts"] / 1000), ev["nickname"],
                         "contact" if kind == "DWS" else "motion", f"{ev['nickname']} {text}", None, None, 0))
        except (KeyError, TypeError, ValueError):
            continue  # a battery or other reading, not an open/close or motion
    with _lock, _db() as db:
        before = db.total_changes
        db.executemany("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?)", rows)
        return db.total_changes - before


def poll_sensors(api, now: float | None = None) -> int:
    """Every few minutes, fetch the last day of sensor events (overlap is harmless: ids are unique)."""
    global _last_poll
    now = time.time() if now is None else now
    if now - _last_poll < SENSOR_POLL:
        return 0
    _last_poll = now
    return save_sensor_events(api.get_sensor_events(int(now - 86400) * 1000))


def prune() -> int:
    """Delete the oldest events and their files while the archive is over its size limit. Returns events removed."""
    limit = int(EVENT_ARCHIVE_GB * 1024**3)
    if not limit:
        return 0
    removed = 0
    with _lock, _db() as db:
        total = db.execute("SELECT COALESCE(SUM(size),0) FROM events").fetchone()[0]
        for row in db.execute("SELECT id, size FROM events ORDER BY ts, id").fetchall():
            if total <= limit:
                break
            _remove(db, row["id"])
            total -= row["size"]
            removed += 1
    if removed:
        logger.info(f"[ARCHIVE] Deleted {removed} of the oldest events to stay under the size limit")
    return removed


def _remove(db: sqlite3.Connection, eid: str) -> None:
    for ext in ("jpg", "mp4"):
        (ARCHIVE_DIR / "media" / f"{eid}.{ext}").unlink(missing_ok=True)
    db.execute("DELETE FROM events WHERE id=?", (eid,))


def delete(eid: str) -> bool:
    if not ID_RE.fullmatch(eid):
        return False
    with _lock, _db() as db:
        found = db.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone()
        _remove(db, eid)
    return bool(found)


def clear() -> None:
    with _lock, _db() as db:
        for row in db.execute("SELECT id FROM events").fetchall():
            _remove(db, row["id"])


def search(device: str = "", kind: str = "", since: int = 0, until: int = 0, q: str = "", before: int = 0, limit: int = 60) -> dict:
    """Newest first. before is the ts of the last row already shown, for the next page."""
    where, args = ["1=1"], []
    for clause, value in (("device=?", device), ("type=?", kind), ("ts>=?", since), ("ts<?", until), ("ts<?", before)):
        if value:
            where.append(clause)
            args.append(value)
    if q:
        where.append("text LIKE ? ESCAPE '\\'")
        args.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    with _db() as db:
        rows = db.execute(
            f"SELECT id, ts, device, type, text, img IS NOT NULL AS img, clip IS NOT NULL AS clip FROM events WHERE {' AND '.join(where)}"
            " ORDER BY ts DESC, id LIMIT ?",
            (*args, max(1, min(limit, 200))),
        ).fetchall()
        devices = [r[0] for r in db.execute("SELECT DISTINCT device FROM events ORDER BY device")]
        types = [r[0] for r in db.execute("SELECT DISTINCT type FROM events ORDER BY type")]
    return {"events": [dict(r) for r in rows], "devices": devices, "types": types}


def counts(since: int) -> dict:
    """How many events of each type since a time, for the Live page's summary."""
    with _db() as db:
        return dict(db.execute("SELECT type, COUNT(*) FROM events WHERE ts>=? GROUP BY type", (since,)).fetchall())


def media_file(eid: str, kind: str) -> Path | None:
    """The saved picture ("img") or clip ("clip") of an event, or None. Only ever inside the archive's media folder."""
    if kind not in ("img", "clip") or not ID_RE.fullmatch(eid):
        return None
    with _db() as db:
        row = db.execute(f"SELECT {kind} FROM events WHERE id=?", (eid,)).fetchone()
    path = ARCHIVE_DIR / "media" / (row[0] if row and row[0] else "")
    return path if row and row[0] and path.is_file() else None
