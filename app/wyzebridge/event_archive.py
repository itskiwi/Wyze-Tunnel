"""Keeps every camera event's picture and clip (Wyze deletes its own after 14 days), plus sensor history, for the History page."""

import hashlib
import mmap
import struct
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
COLS = "id, ts, device, type, text, img, clip, size, uri, tags, dur"
# Added after the first release; old archives get them on open.
NEW_COLS = {"uri": "TEXT", "tags": "TEXT", "dur": "INTEGER", "kept": "INTEGER DEFAULT 0"}
REPEAT_GAP = 20 * 60  # the same camera and kind again within this folds into one group
VISIT_GAP = 90  # a person or vehicle on another camera this soon after is the same visit
VISIT_KINDS = ("person", "vehicle")


def _db() -> sqlite3.Connection:
    (ARCHIVE_DIR / "media").mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(ARCHIVE_DIR / "index.db", timeout=10)
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, ts INTEGER, device TEXT, type TEXT, text TEXT,"
        " img TEXT, clip TEXT, size INTEGER)"
    )
    have = {r[1] for r in db.execute("PRAGMA table_info(events)")}
    for col, kind in NEW_COLS.items():
        if col not in have:
            db.execute(f"ALTER TABLE events ADD COLUMN {col} {kind}")
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


def mp4_seconds(path: Path) -> int:
    """A clip's length from its mp4 header (the mvhd box), or 0 if it can't be read."""
    try:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            if (i := m.find(b"mvhd")) < 0:
                return 0
            if m[i + 4] == 1:  # version 1: 64-bit times
                scale, length = struct.unpack(">IQ", m[i + 24 : i + 36])
            else:
                scale, length = struct.unpack(">II", m[i + 16 : i + 24])
            return round(length / scale) if scale else 0
    except (OSError, ValueError, struct.error):
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
    kind = entry.get("label") or "motion"
    tags = ",".join(t for t in entry.get("tags") or [kind] if t != "motion")
    row = (eid, int(entry["ts"]), entry["name"], kind, entry["text"], img, clip, n1 + n2, entry.get("uri"), tags, mp4_seconds(media / clip) if clip else 0)
    with _lock, _db() as db:
        db.execute(f"INSERT OR IGNORE INTO events ({COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
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
                         "contact" if kind == "DWS" else "motion", f"{ev['nickname']} {text}", None, None, 0, None, None, 0))
        except (KeyError, TypeError, ValueError):
            continue  # a battery or other reading, not an open/close or motion
    with _lock, _db() as db:
        before = db.total_changes
        db.executemany(f"INSERT OR IGNORE INTO events ({COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
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
        # Kept events are never pruned; if they alone are over the limit, the archive stays over it.
        for row in db.execute("SELECT id, size FROM events WHERE NOT kept ORDER BY ts, id").fetchall():
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


def keep(eid: str, kept: bool) -> bool:
    """Star an event so the size limit never deletes it."""
    if not ID_RE.fullmatch(eid):
        return False
    with _lock, _db() as db:
        return db.execute("UPDATE events SET kept=? WHERE id=?", (int(kept), eid)).rowcount > 0


def clear() -> None:
    with _lock, _db() as db:
        for row in db.execute("SELECT id FROM events").fetchall():
            _remove(db, row["id"])


def search(device: str = "", kind: str = "", since: int = 0, until: int = 0, q: str = "", before: int = 0, limit: int = 60) -> dict:
    """Newest first. before is the ts of the last row already shown, for the next page.
    counts (per kind) and times (every event's ts, for the day bars and the 24 hour strip) ignore the kind and paging, so the chips show what else is there."""
    where, args = ["1=1"], []
    for clause, value in (("device=?", device), ("ts>=?", since), ("ts<?", until)):
        if value:
            where.append(clause)
            args.append(value)
    if q:
        where.append("(text LIKE ? ESCAPE '\\' OR device LIKE ? ESCAPE '\\')")
        args += ["%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"] * 2
    page_where, page_args = list(where), list(args)
    for clause, value in (("type=?", kind), ("ts<?", before)):
        if value:
            page_where.append(clause)
            page_args.append(value)
    with _db() as db:
        rows = db.execute(
            f"SELECT id, ts, device, type, text, img IS NOT NULL AS img, clip IS NOT NULL AS clip, uri, tags, dur, kept FROM events"
            f" WHERE {' AND '.join(page_where)} ORDER BY ts DESC, id LIMIT ?",
            (*page_args, max(1, min(limit, 300))),
        ).fetchall()
        sql = " AND ".join(where)
        counts = dict(db.execute(f"SELECT type, COUNT(*) FROM events WHERE {sql} GROUP BY type", args).fetchall())
        # shortcut: only with a start date, capped at 5000; bucket on the server if a range ever needs more.
        times = [r[0] for r in db.execute(f"SELECT ts FROM events WHERE {sql} ORDER BY ts DESC LIMIT 5000", args)] if since else []
        devices = [r[0] for r in db.execute("SELECT DISTINCT device FROM events ORDER BY device")]
        types = [r[0] for r in db.execute("SELECT DISTINCT type FROM events ORDER BY type")]
    events = [dict(r) | {"tags": (r["tags"] or "").split(",") if r["tags"] else [], "kept": bool(r["kept"])} for r in rows]
    return {"events": events, "groups": group(events), "counts": counts, "times": times, "devices": devices, "types": types}


def group(events: list[dict], gap: int = REPEAT_GAP, visit_gap: int = VISIT_GAP) -> list[dict]:
    """Fold events into groups, newest first. The same camera and kind again within gap is one group (a cat pacing the yard);
    a person or vehicle seen on another camera within visit_gap of the last sighting joins it too (one visit, cameras in order).
    Each group: kind, first, last, count, ids (newest first), cameras {device: [first, last]} in the order they were seen."""
    groups: list[dict] = []
    for e in sorted(events, key=lambda e: (e["ts"], e["id"])):
        for g in reversed(groups[-20:]):
            near = e["ts"] - g["last"]
            if g["kind"] == e["type"] and (e["device"] in g["cameras"] and near <= gap or e["type"] in VISIT_KINDS and near <= visit_gap):
                break
        else:
            g = {"kind": e["type"], "first": e["ts"], "last": e["ts"], "count": 0, "ids": [], "cameras": {}}
            groups.append(g)
        g["last"], g["count"] = e["ts"], g["count"] + 1
        g["ids"].insert(0, e["id"])
        g["cameras"].setdefault(e["device"], [e["ts"], e["ts"]])[1] = e["ts"]
    return sorted(groups, key=lambda g: g["last"], reverse=True)


def media_file(eid: str, kind: str) -> Path | None:
    """The saved picture ("img") or clip ("clip") of an event, or None. Only ever inside the archive's media folder."""
    if kind not in ("img", "clip") or not ID_RE.fullmatch(eid):
        return None
    with _db() as db:
        row = db.execute(f"SELECT {kind} FROM events WHERE id=?", (eid,)).fetchone()
    path = ARCHIVE_DIR / "media" / (row[0] if row and row[0] else "")
    return path if row and row[0] and path.is_file() else None
