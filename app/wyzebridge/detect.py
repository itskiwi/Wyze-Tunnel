"""Spot people, cars and animals in recording cameras, or read them from Frigate, so footage can be searched.

Built-in detection runs MobileNet-SSD (MIT, github.com/chuanqi305/MobileNet-SSD) on keyframes only, so it stays
light enough for a Raspberry Pi. Set FRIGATE_URL to use Frigate's detections instead (its own models, including
packages with Frigate+).
"""

import sqlite3
import subprocess
import time
from datetime import timedelta
from pathlib import Path
from threading import Event, Lock, Thread

import requests

try:
    import cv2
    import numpy as np
except ImportError:  # no wheels for 32-bit ARM, so built-in detection is off there
    cv2 = None

from wyzebridge.bridge_utils import env_bool
from wyzebridge.config import IMG_PATH, TOKEN_PATH
from wyzebridge.ffmpeg import parse_timedelta
from wyzebridge.logging import logger

FRIGATE_URL = env_bool("FRIGATE_URL", style="original").rstrip("/")
MIN_SCORE = float(env_bool("DETECT_SCORE") or 0.6)
COOLDOWN = 60  # one row per camera and label a minute, not one per keyframe
KEEP = parse_timedelta("DETECT_KEEP") or parse_timedelta("RECORD_KEEP") or timedelta(days=30)
DB_FILE = TOKEN_PATH + "detections.db"
THUMB_DIR = Path(IMG_PATH, "detections")
MODEL_DIR = Path(TOKEN_PATH, "models")
MODEL_URL = "https://raw.githubusercontent.com/chuanqi305/MobileNet-SSD/master/"
MODEL_FILES = {"deploy.prototxt": 44667, "mobilenet_iter_73000.caffemodel": 23306119}
# MobileNet-SSD's Pascal VOC classes, keeping the ones a home camera cares about.
LABELS = {
    2: "bicycle",
    3: "bird",
    6: "bus",
    7: "car",
    8: "cat",
    12: "dog",
    14: "motorcycle",
    15: "person",
}
WIDTH, HEIGHT = 640, 360

_db_lock = Lock()
_model_lock = Lock()
_watchers: dict[str, "Watcher"] = {}


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE IF NOT EXISTS detections (id INTEGER PRIMARY KEY, cam TEXT, label TEXT, score REAL,"
        " ts REAL, source TEXT, ext_id TEXT UNIQUE)"
    )
    con.execute("CREATE INDEX IF NOT EXISTS detections_ts ON detections (ts)")
    return con


def add(
    cam: str,
    label: str,
    score: float,
    ts: float,
    source: str = "local",
    ext_id: str | None = None,
) -> int | None:
    """Save a detection; returns its id, or None if this Frigate event is already saved."""
    with _db_lock, _db() as con:
        cur = con.execute(
            "INSERT OR IGNORE INTO detections (cam, label, score, ts, source, ext_id) VALUES (?, ?, ?, ?, ?, ?)",
            (cam, label, round(score, 2), ts, source, ext_id),
        )
        for row in con.execute(
            "SELECT id FROM detections WHERE ts < ?",
            (time.time() - KEEP.total_seconds(),),
        ):
            (THUMB_DIR / f"{row['id']}.jpg").unlink(missing_ok=True)
        con.execute("DELETE FROM detections WHERE ts < ?", (time.time() - KEEP.total_seconds(),))
        return cur.lastrowid if cur.rowcount else None


def search(label: str = "", cam: str = "", before: float = 0, limit: int = 120) -> list[dict]:
    """Newest first. Filter by label and camera; page back with `before` (a ts)."""
    query, args = (
        "SELECT id, cam, label, score, ts, source, ext_id FROM detections WHERE 1",
        [],
    )
    for col, val in (("label", label), ("cam", cam)):
        if val:
            query += f" AND {col} = ?"
            args.append(val)
    if before:
        query += " AND ts < ?"
        args.append(before)
    with _db_lock, _db() as con:
        rows = con.execute(query + " ORDER BY ts DESC LIMIT ?", (*args, limit)).fetchall()
    return [dict(r) for r in rows]


def get(det_id: int) -> dict | None:
    with _db_lock, _db() as con:
        row = con.execute("SELECT * FROM detections WHERE id = ?", (det_id,)).fetchone()
    return dict(row) if row else None


def labels() -> list[str]:
    with _db_lock, _db() as con:
        return [r[0] for r in con.execute("SELECT label FROM detections GROUP BY label ORDER BY COUNT(*) DESC")]


def enabled() -> bool:
    return bool(FRIGATE_URL or _watchers)


def load_model():
    """Download the model once (23 MB) and load it. Returns None if OpenCV isn't installed or the download fails."""
    if not cv2:
        logger.warning("[DETECT] OpenCV isn't installed on this system, so built-in detection is off.")
        return None
    with _model_lock:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        for name, size in MODEL_FILES.items():
            path = MODEL_DIR / name
            if path.exists() and path.stat().st_size == size:
                continue
            logger.info(f"[DETECT] Downloading the detection model: {name}")
            try:
                resp = requests.get(MODEL_URL + name, timeout=60)
                resp.raise_for_status()
            except requests.RequestException as ex:
                logger.error(f"[DETECT] Couldn't download the detection model: {ex}")
                return None
            path.with_suffix(".part").write_bytes(resp.content)
            path.with_suffix(".part").replace(path)
    model = cv2.dnn_DetectionModel(
        str(MODEL_DIR / "deploy.prototxt"),
        str(MODEL_DIR / "mobilenet_iter_73000.caffemodel"),
    )
    model.setInputParams(scale=1 / 127.5, size=(300, 300), mean=(127.5, 127.5, 127.5))
    return model


def find_objects(model, frame) -> dict[str, float]:
    """Best score per label in one frame."""
    found: dict[str, float] = {}
    class_ids, scores, _ = model.detect(frame, MIN_SCORE)
    for class_id, score in zip(class_ids, scores):
        if label := LABELS.get(int(class_id)):
            found[label] = max(found.get(label, 0), float(score))
    return found


class Watcher(Thread):
    """Read keyframes from one camera's local RTSP stream and save what it sees."""

    def __init__(self, uri: str, name: str, on_detect=None):
        super().__init__(daemon=True, name=f"detect-{uri}")
        self.uri, self.cam_name, self.on_detect = uri, name, on_detect
        self.stop_flag = Event()
        self.proc: subprocess.Popen | None = None
        self.last_seen: dict[str, float] = {}

    def run(self):
        if not (model := load_model()):
            return
        logger.info(f"[DETECT] Watching {self.uri} for people, cars and animals")
        while not self.stop_flag.is_set():
            self.proc = subprocess.Popen(self.ffmpeg_cmd(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            frame_size = WIDTH * HEIGHT * 3
            while not self.stop_flag.is_set() and len(raw := self.proc.stdout.read(frame_size)) == frame_size:
                self.check(model, np.frombuffer(raw, np.uint8).reshape(HEIGHT, WIDTH, 3))
            self.proc.kill()
            self.stop_flag.wait(10)  # the stream is down or restarting

    def ffmpeg_cmd(self) -> list[str]:
        # Decoding keyframes only keeps this to a few percent of a CPU core per camera.
        return (
            [
                "ffmpeg",
                "-loglevel",
                "fatal",
                "-skip_frame",
                "nokey",
                "-rtsp_transport",
                "tcp",
            ]
            + ["-i", f"rtsp://127.0.0.1:8554/{self.uri}", "-map", "0:v:0", "-an"]
            + [
                "-vf",
                f"scale={WIDTH}:{HEIGHT}",
                "-pix_fmt",
                "bgr24",
                "-f",
                "rawvideo",
                "-",
            ]
        )

    def check(self, model, frame):
        now = time.time()
        for label, score in find_objects(model, frame).items():
            if now - self.last_seen.get(label, 0) < COOLDOWN:
                self.last_seen[label] = now  # still there: extend, don't repeat
                continue
            self.last_seen[label] = now
            if det_id := add(self.uri, label, score, now):
                THUMB_DIR.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(
                    str(THUMB_DIR / f"{det_id}.jpg"),
                    frame,
                    [cv2.IMWRITE_JPEG_QUALITY, 80],
                )
                logger.info(f"[DETECT] {label.capitalize()} on {self.uri} ({score:.0%})")
                if self.on_detect:
                    self.on_detect(self.uri, self.cam_name, label, det_id, now)

    def stop(self):
        self.stop_flag.set()
        if self.proc:
            self.proc.kill()


def watch(uri: str, name: str, on_detect=None) -> None:
    """Start watching a camera once; calling again after a bridge restart is a no-op."""
    if FRIGATE_URL or (uri in _watchers and _watchers[uri].is_alive()):
        return
    _watchers[uri] = Watcher(uri, name, on_detect)
    _watchers[uri].start()


def stop_all() -> None:
    for watcher in _watchers.values():
        watcher.stop()


def frigate_uri(camera: str, uris: list[str]) -> str:
    """Frigate names cameras like front_door; ours are front-door. Match them so clips line up."""
    return next(
        (uri for uri in uris if uri.replace("-", "_") == camera.lower().replace("-", "_")),
        camera,
    )


def poll_frigate(uris: list[str], since: float) -> float:
    """Save Frigate's finished events newer than `since`; returns the newest start time seen."""
    resp = requests.get(f"{FRIGATE_URL}/api/events", params={"after": since, "limit": 100}, timeout=10)
    resp.raise_for_status()
    for event in resp.json():
        score = event.get("top_score") or (event.get("data") or {}).get("top_score") or 0
        add(
            frigate_uri(event["camera"], uris),
            event["label"],
            score,
            event["start_time"],
            "frigate",
            event["id"],
        )
        since = max(since, event["start_time"])
    return since


def start_frigate(get_uris) -> None:
    if not FRIGATE_URL:
        return

    def loop():
        since = time.time() - 86400
        logger.info(f"[DETECT] Reading detections from Frigate at {FRIGATE_URL}")
        while True:
            try:
                since = poll_frigate(get_uris(), since)
            except (requests.RequestException, ValueError, KeyError) as ex:
                logger.warning(f"[DETECT] Couldn't read events from Frigate: {ex}")
            time.sleep(30)

    Thread(target=loop, daemon=True, name="frigate").start()


def frigate_media(ext_id: str, kind: str) -> requests.Response:
    """Stream a Frigate event's thumbnail.jpg or clip.mp4."""
    return requests.get(f"{FRIGATE_URL}/api/events/{ext_id}/{kind}", stream=True, timeout=10)
