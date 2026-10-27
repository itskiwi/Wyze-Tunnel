"""Keeps every camera event's picture and clip (Wyze deletes its own after 14 days), plus sensor history, for the History page."""

import calendar
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
BASELINE_DAYS = 30
BASELINE_MIN_DAYS = 7
# kinds worth lifting when they've never happened at that camera and hour, and how to say so
UNUSUAL_KINDS = {"person": "Nobody has been", "vehicle": "No vehicle has been", "package": "No package has been left",
                 "contact": "{device} hasn't opened", "glass": "No breaking glass",
                 "gunshot": "No loud bang"}
ALWAYS_SHOW = ("smoke", "co", "glass", "gunshot", "rang the doorbell", "package")  # never folded away as usual


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


def keep(eid: str, kept: bool) -> dict | None:
    """Star an event so the size limit never deletes it. Returns its camera uri and time, or None if there's no such event."""
    if not ID_RE.fullmatch(eid):
        return None
    with _lock, _db() as db:
        if not db.execute("UPDATE events SET kept=? WHERE id=?", (int(kept), eid)).rowcount:
            return None
        return dict(db.execute("SELECT uri, ts FROM events WHERE id=?", (eid,)).fetchone())


def clear() -> None:
    with _lock, _db() as db:
        for row in db.execute("SELECT id FROM events").fetchall():
            _remove(db, row["id"])


def search(device: str = "", kind: str = "", since: int = 0, until: int = 0, q: str = "", before: int = 0, limit: int = 60, tz: int | None = None) -> dict:
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
    groups = group(events)
    if since:
        judge(groups, since, tz=tz)
    return {"events": events, "groups": groups, "counts": counts, "times": times, "devices": devices, "types": types}


def _local(ts: float, tz: int | None = None) -> time.struct_time:
    """Local time: the browser's when it sent its offset (JavaScript getTimezoneOffset, minutes), else the bridge's."""
    return time.localtime(ts) if tz is None else time.gmtime(ts - tz * 60)


def _hours(ts: float, tz: int | None = None) -> float:
    t = _local(ts, tz)
    return t.tm_hour + t.tm_min / 60


def judge(groups: list[dict], before: int, days: int = BASELINE_DAYS, tz: int | None = None) -> None:
    """Mark groups against the same camera, kind and time of day over the days before `before` (the day shown):
    "why" (a sentence) when it has never happened then and it's the kind people act on, "usual" when it happens most days.
    Needs a week of history; until then nothing is marked."""
    with _db() as db:
        oldest = db.execute("SELECT MIN(ts) FROM events").fetchone()[0]
        if not oldest or before - oldest < BASELINE_MIN_DAYS * 86400:
            return
        rows = db.execute("SELECT ts, device, type FROM events WHERE ts>=? AND ts<?", (before - days * 86400, before)).fetchall()
    span = min(days, round((before - oldest) / 86400))
    seen: dict[tuple, set] = {}
    active: dict[str, set] = {}
    for ts, device, kind in rows:
        day = time.strftime("%Y%m%d", _local(ts, tz))
        seen.setdefault((device, kind), set()).add((day, _hours(ts, tz)))
        active.setdefault(device, set()).add(day)
    for g in groups:
        device, hour = next(iter(g["cameras"])), _hours(g["first"], tz)
        if len(active.get(device, ())) < BASELINE_MIN_DAYS:
            continue  # a new or quiet camera: too little to say what's normal
        # within an hour either side of the time of day, wrapping round midnight
        days_seen = {d for d, h in seen.get((device, g["kind"]), ()) if min(abs(h - hour), 24 - abs(h - hour)) <= 1}
        if not days_seen and g["kind"] in UNUSUAL_KINDS:
            what = UNUSUAL_KINDS[g["kind"]]
            where = "" if "{device}" in what else f" at {device}"
            g["why"] = f"{what.format(device=device)}{where} at this time of day in the last {span} days."
        elif len(days_seen) >= max(4, span / 3) and g["kind"] not in ALWAYS_SHOW:
            g["usual"] = True


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


# "Just ask": plain questions answered from the archive, no model. Longest phrases first so "doorbell" wins over "door".
ASK_KINDS = [
    ("rang the doorbell", ("doorbell", "ring", "rang")), ("package", ("package", "parcel", "delivery", "delivered", "box", "amazon", "mail")),
    ("vehicle", ("vehicle", "car", "truck", "van", "drove", "drive")), ("pet", ("pet", "dog", "cat", "animal")),
    ("barking", ("bark",)), ("contact", ("door", "opened", "open")),
    ("person", ("person", "someone", "somebody", "anyone", "anybody", "people", "visitor", "who", "man", "woman", "kid")),
]
ASK_WORDS = ("did", "was", "were", "is", "are", "has", "have", "any", "when", "how", "who", "what")


def is_question(q: str) -> bool:
    words = re.findall(r"[a-z']+", q.lower())
    return q.strip().endswith("?") or bool(words and words[0] in ASK_WORDS)


def _window(q: str, now: float, tz: int | None = None) -> tuple[int, int, str]:
    """The time a question is about, from its words: (since, until, how to say it)."""
    t = _local(now, tz)

    def day(n: int) -> int:  # midnight n days from today; mktime keeps it right across DST
        if tz is None:
            return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday + n, 0, 0, 0, 0, 0, -1)))
        return int(calendar.timegm((t.tm_year, t.tm_mon, t.tm_mday + n, 0, 0, 0, 0, 0, 0))) + tz * 60

    today = day(0)
    if "last night" in q:
        return day(-1) + 18 * 3600, today + 6 * 3600, "last night"
    if "yesterday" in q:
        return day(-1), today, "yesterday"
    if "this morning" in q:
        return today, today + 12 * 3600, "this morning"
    if "tonight" in q or "this evening" in q:
        return today + 17 * 3600, day(1), "this evening"
    if "today" in q:
        return today, day(1), "today"
    if "week" in q:
        return day(-6), day(1), "this week"
    return day(-6), day(1), "in the last week"


def ask(q: str, now: float | None = None, tz: int | None = None) -> dict:
    """Answer a question like "did a package come today?" or "when did the car leave?" with a sentence and the events behind it."""
    now = time.time() if now is None else now
    text = " " + re.sub(r"[^a-z0-9' ]", " ", q.lower()) + " "
    with _db() as db:
        devices = [r[0] for r in db.execute("SELECT DISTINCT device FROM events")]
    device = max((d for d in devices if f" {d.lower()} " in text), key=len, default="")
    if device:
        text = text.replace(f" {device.lower()} ", " ")
    kind = next((k for k, words in ASK_KINDS if any(re.search(rf"\b{w}", text) for w in words)), "")
    since, until, when = _window(text, now, tz)
    got = search(device, kind, since, until, limit=200, tz=tz)
    events, groups = got["events"], got["groups"]
    thing = {"rang the doorbell": "doorbell rings", "contact": "door openings", "": "events"}.get(kind, f"{kind} events")
    at = f" at {device}" if device else ""
    if not events:
        answer = f"No, no {thing}{at} {when}." if kind or device else f"Nothing {when}."
    else:
        latest = events[0]
        said = f"{latest['text'].rstrip('.')} at {time.strftime('%I:%M %p', _local(latest['ts'], tz)).lstrip('0')}"
        if when not in ("today", "this morning", "this evening"):
            said += time.strftime(" on %A", _local(latest["ts"], tz))
        words = re.findall(r"[a-z]+", text)
        if words[:2] == ["how", "many"] or words[:2] == ["how", "often"]:
            answer = f"{len(events)} {thing}{at} {when}. The latest: {said}."
        elif words[:1] == ["when"]:
            answer = f"{said}." if len(events) == 1 else f"{said}, the latest of {len(events)} {when}."
        else:
            answer = f"Yes. {said}." if len(events) == 1 else f"Yes, {len(events)} {thing} {when}. The latest: {said}."
    return {"answer": answer, "kind": kind, "device": device, "since": since, "until": until, "events": events[:50], "groups": groups[:20]}
