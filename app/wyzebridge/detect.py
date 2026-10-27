"""Spot people, cars and animals in recording cameras, or read them from Frigate, so footage can be searched.

Built-in detection runs MobileNet-SSD (MIT, github.com/chuanqi305/MobileNet-SSD) on keyframes only, so it stays
light enough for a Raspberry Pi. A stronger model, YOLOX-S (Apache-2.0, github.com/Megvii-BaseDetection/YOLOX), can be
picked instead on a PC: it finds smaller and farther things, at a few times the CPU. A Frigate address (settings or FRIGATE_URL) uses Frigate's detections instead (its
own models, including packages with Frigate+). Cameras and the Frigate address are set on the Search footage page.
"""

import json
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import timedelta
from pathlib import Path
from threading import Event, Lock, Thread

import requests

# No wheels for 32-bit ARM, and NumPy refuses CPUs older than x86-64-v2 with a RuntimeError: detection is off there.
try:
    import cv2
    import numpy as np
except (ImportError, RuntimeError):
    cv2 = None

from wyzebridge import recording
from wyzebridge.bridge_utils import env_bool, env_cam
from wyzebridge.config import IMG_PATH, TOKEN_PATH
from wyzebridge.ffmpeg import parse_timedelta
from wyzebridge.logging import logger

FRIGATE_URL = env_bool("FRIGATE_URL", style="original").rstrip("/")
SETTINGS_FILE = TOKEN_PATH + "detect.json"
MIN_SCORE = float(env_bool("DETECT_SCORE") or 0.6)
COOLDOWN = 60  # one row per camera and label a minute, not one per keyframe
CLEAR_AFTER = 120  # a person not seen for this long has gone
VEHICLES = {"car", "truck", "bus", "motorcycle"}
KEEP = parse_timedelta("DETECT_KEEP") or parse_timedelta("RECORD_KEEP") or timedelta(days=7)
# Cameras switched on in the web UI record too, so every hit has a clip; keep those clips as long as the hits.
AUTO_RECORD_KEEP = f"{int(KEEP.total_seconds())}s"
DB_FILE = TOKEN_PATH + "detections.db"
THUMB_DIR = Path(IMG_PATH, "detections")
MODEL_DIR = Path(TOKEN_PATH, "models")
# Each model: where its files download from, and their exact sizes (a short file is a broken download).
MODELS = {
    "standard": (
        "https://raw.githubusercontent.com/chuanqi305/MobileNet-SSD/master/",
        {"deploy.prototxt": 44667, "mobilenet_iter_73000.caffemodel": 23306119},
    ),
    "strong": (
        "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/",
        {"yolox_s.onnx": 35858002},
    ),
}
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
# YOLOX was trained on COCO: the same kinds of things, numbered differently, plus trucks.
COCO_LABELS = {
    0: "person",
    1: "bicycle",
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
    14: "bird",
    15: "cat",
    16: "dog",
}
WIDTH, HEIGHT = 640, 360

_db_lock = Lock()
_model_lock = Lock()
_watchers: dict[str, "Watcher"] = {}
# Motion heatmap: centers of everything spotted, counted on a coarse grid over the picture.
HEAT_W, HEAT_H = 16, 9
HEAT_FILE = TOKEN_PATH + "heatmap.json"
HEAT: dict[str, list[int]] = {}
_heat_lock = Lock()
_heat_saved = 0.0
# Activity zones: heat grid cells to ignore per camera (a busy road, a tree), so they don't fill Search or send alerts.
ZONES_FILE = TOKEN_PATH + "zones.json"


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
        if cur.rowcount:
            recording.mark(cam, ts)
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


def load_settings() -> dict:
    """{"cams": [uri, ...], "frigate_url": str} saved from the web UI."""
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_settings(form: dict, uris: list[str]) -> dict:
    """Validate and save. Raises ValueError with a message for the user."""
    model = form.get("model") or "standard"
    if model not in MODELS:
        raise ValueError("Pick a detection model from the list")
    url = str(form.get("frigate_url") or "").strip().rstrip("/")
    if url and not re.fullmatch(r"https?://[^\s/]+(/[^\s]*)?", url):
        raise ValueError("The Frigate address should look like http://192.168.1.20:5000")
    cams = form.get("cams") or []
    if not isinstance(cams, list) or not all(isinstance(c, str) for c in cams):
        raise ValueError("Pick cameras from the list")
    settings = {"cams": [c for c in cams if c in uris], "frigate_url": url, "model": model}
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f)
    return settings


def frigate_url() -> str:
    return load_settings().get("frigate_url") or FRIGATE_URL


def wanted(uri: str) -> bool:
    """Built-in detection for this camera: switched on in the web UI or with DETECT_<CAM>."""
    return not frigate_url() and bool(cv2) and (uri in load_settings().get("cams", []) or bool(env_cam("detect", uri)))


def enabled() -> bool:
    return bool(frigate_url() or any(not w.stop_flag.is_set() for w in _watchers.values()))


def load_model(name: str = ""):
    """Download the picked model once and load it. Returns None if OpenCV isn't installed or the download fails."""
    if not cv2:
        logger.warning("[DETECT] OpenCV isn't installed on this system, so built-in detection is off.")
        return None
    name = name if name in MODELS else load_settings().get("model", "standard")
    url, files = MODELS.get(name, MODELS["standard"])
    with _model_lock:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        for file, size in files.items():
            path = MODEL_DIR / file
            if path.exists() and path.stat().st_size == size:
                continue
            logger.info(f"[DETECT] Downloading the detection model: {file}")
            try:
                resp = requests.get(url + file, timeout=60)
                resp.raise_for_status()
            except requests.RequestException as ex:
                logger.error(f"[DETECT] Couldn't download the detection model: {ex}")
                return None
            path.with_suffix(".part").write_bytes(resp.content)
            path.with_suffix(".part").replace(path)
    if "yolox_s.onnx" in files:
        return Yolox(cv2.dnn.readNetFromONNX(str(MODEL_DIR / "yolox_s.onnx")))
    model = cv2.dnn_DetectionModel(
        str(MODEL_DIR / "deploy.prototxt"),
        str(MODEL_DIR / "mobilenet_iter_73000.caffemodel"),
    )
    model.setInputParams(scale=1 / 127.5, size=(300, 300), mean=(127.5, 127.5, 127.5))
    return model


class Yolox:
    """YOLOX-S behind the same detect() as OpenCV's DetectionModel: class ids, scores and x, y, w, h boxes."""

    labels = COCO_LABELS
    SIZE = 640

    def __init__(self, net):
        self.net = net
        grids, strides = [], []
        for stride in (8, 16, 32):
            n = self.SIZE // stride
            ys, xs = np.mgrid[0:n, 0:n]
            grids.append(np.stack((xs, ys), -1).reshape(-1, 2))
            strides.append(np.full((n * n, 1), stride))
        self.grid, self.stride = np.concatenate(grids), np.concatenate(strides)

    def detect(self, frame, min_score):
        # Pad (not stretch) to a square, gray like YOLOX's own demo; frames are 640 wide so no resize is needed.
        h, w = frame.shape[:2]
        scale = min(self.SIZE / h, self.SIZE / w)
        padded = np.full((self.SIZE, self.SIZE, 3), 114, np.uint8)
        padded[: int(h * scale), : int(w * scale)] = cv2.resize(frame, (int(w * scale), int(h * scale)))
        self.net.setInput(cv2.dnn.blobFromImage(padded))
        out = self.net.forward()[0]
        centers = (out[:, :2] + self.grid) * self.stride
        sizes = np.exp(out[:, 2:4]) * self.stride
        class_ids = out[:, 5:].argmax(1)
        scores = out[:, 4] * out[np.arange(len(out)), 5 + class_ids]
        keep = scores >= min_score
        boxes = (np.hstack((centers - sizes / 2, sizes))[keep] / scale).round().astype(int).tolist()
        class_ids, scores = class_ids[keep].tolist(), scores[keep].tolist()
        picked = cv2.dnn.NMSBoxesBatched(boxes, scores, class_ids, min_score, 0.45) if boxes else []
        return [class_ids[i] for i in picked], [scores[i] for i in picked], [boxes[i] for i in picked]


def find_objects(model, frame, heat: list[int] | None = None, ignore=frozenset(), best: dict | None = None) -> dict[str, float]:
    """Best score per label in one frame, skipping objects centered in ignored cells.

    Adds every object's center to the heat grid if given, ignored or not, so the heatmap still shows the busy spots.
    Fills `best` with each label's best x, y, w, h if given.
    """
    found: dict[str, float] = {}
    class_ids, scores, boxes = model.detect(frame, MIN_SCORE)
    names = getattr(model, "labels", LABELS)
    for class_id, score, (x, y, w, h) in zip(class_ids, scores, boxes):
        if label := names.get(int(class_id)):
            col = min(max(int((x + w / 2) / WIDTH * HEAT_W), 0), HEAT_W - 1)
            row = min(max(int((y + h / 2) / HEIGHT * HEAT_H), 0), HEAT_H - 1)
            if heat is not None:
                heat[row * HEAT_W + col] += 1
            if row * HEAT_W + col not in ignore and score > found.get(label, 0):
                found[label] = float(score)
                if best is not None:
                    best[label] = (int(x), int(y), int(w), int(h))
    return found


def soft_glow(frame, box: tuple):
    """A soft, warm light on the subject instead of a hard box: the rest of the picture dims a little."""
    x, y, w, h = box
    mask = np.zeros(frame.shape[:2], np.float32)
    cv2.ellipse(mask, (x + w // 2, y + h // 2), (max(w * 2 // 3, 24), max(h * 2 // 3, 24)), 0, 0, 360, 1.0, -1)
    mask = cv2.GaussianBlur(mask, (0, 0), max(w, h, 40) / 4)[..., None]
    dim = frame * np.array([0.62, 0.68, 0.76], np.float32)  # BGR: darker and a touch amber
    return (frame * mask + dim * (1 - mask)).astype(np.uint8)


REEL_DIR = Path(IMG_PATH, "reels")


def reel(cam: str = "", hours: int = 24) -> Path | None:
    """A short video of the last day's hits, one picture a second, oldest first. Remade at most every 10 minutes."""
    out = REEL_DIR / f"{cam or 'all'}.mp4"
    if out.exists() and time.time() - out.stat().st_mtime < 600:
        return out
    since = time.time() - hours * 3600
    hits = [h for h in search(cam=cam, limit=500) if h["ts"] >= since and (THUMB_DIR / f"{h['id']}.jpg").exists()]
    if not hits:
        return None
    work = REEL_DIR / f"{cam or 'all'}-frames"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    for i, hit in enumerate(reversed(hits[:120])):
        shutil.copyfile(THUMB_DIR / f"{hit['id']}.jpg", work / f"{i:04d}.jpg")
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-framerate", "1", "-i", str(work / "%04d.jpg")]
    cmd += ["-vf", f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,pad={WIDTH}:{HEIGHT}:-1:-1,fps=12"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    try:
        ok = subprocess.run(cmd, capture_output=True, timeout=300).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    shutil.rmtree(work, ignore_errors=True)
    return out if ok else None


def load_zones() -> dict[str, list[int]]:
    try:
        with open(ZONES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_zone(uri: str, ignore) -> list[int]:
    """Save the cells to ignore on one camera. Raises ValueError with a message for the user."""
    cells = HEAT_W * HEAT_H
    if not isinstance(ignore, list) or not all(isinstance(i, int) and 0 <= i < cells for i in ignore):
        raise ValueError("Pick squares on the picture to ignore.")
    if len(set(ignore)) == cells:
        raise ValueError("That ignores the whole picture. Turn off detection for this camera instead.")
    zones = load_zones()
    zones[uri] = sorted(set(ignore))
    if not zones[uri]:
        del zones[uri]
    with open(ZONES_FILE, "w") as f:
        json.dump(zones, f)
    return zones.get(uri, [])


def load_heat() -> dict[str, list[int]]:
    try:
        with open(HEAT_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def heat_grid(uri: str) -> list[int]:
    """Where things showed up on this camera, as HEAT_W x HEAT_H counts (row by row)."""
    with _heat_lock:
        if not HEAT:
            HEAT.update(load_heat())
        grid = HEAT.setdefault(uri, [0] * HEAT_W * HEAT_H)
        if len(grid) != HEAT_W * HEAT_H:
            grid[:] = [0] * HEAT_W * HEAT_H
        return grid


def save_heat() -> None:
    with _heat_lock, open(HEAT_FILE, "w") as f:
        json.dump(HEAT, f)


class Watcher(Thread):
    """Read keyframes from one camera's local RTSP stream and save what it sees."""

    def __init__(self, uri: str, name: str, on_detect=None):
        super().__init__(daemon=True, name=f"detect-{uri}")
        self.uri, self.cam_name, self.on_detect = uri, name, on_detect
        self.stop_flag = Event()
        self.proc: subprocess.Popen | None = None
        self.last_seen: dict[str, float] = {}
        self.first_seen: dict[str, float] = {}  # start of the current visit, per label

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
        global _heat_saved
        now = time.time()
        boxes: dict[str, tuple] = {}
        found = find_objects(model, frame, heat_grid(self.uri), frozenset(load_zones().get(self.uri, ())), boxes)
        if now - _heat_saved > 300:
            _heat_saved = now
            save_heat()
        if "person" in self.first_seen and "person" not in found and now - self.last_seen["person"] > CLEAR_AFTER:
            del self.first_seen["person"]
            if self.on_detect:
                self.on_detect(self.uri, self.cam_name, "person", None, now, "cleared")
        for label, score in found.items():
            if now - self.last_seen.get(label, 0) < COOLDOWN:
                self.last_seen[label] = now  # still there: extend, don't repeat
                # A vehicle still here after a minute pulled in rather than drove past.
                if label in VEHICLES and now - self.first_seen.get(label, now) >= COOLDOWN:
                    del self.first_seen[label]
                    if self.on_detect:
                        self.on_detect(self.uri, self.cam_name, label, None, now, "stayed")
                continue
            self.last_seen[label] = now
            if label in VEHICLES or label == "person":
                self.first_seen[label] = now
            if det_id := add(self.uri, label, score, now):
                THUMB_DIR.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(
                    str(THUMB_DIR / f"{det_id}.jpg"),
                    soft_glow(frame, boxes[label]) if label in boxes else frame,
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
    if uri in _watchers and not _watchers[uri].stop_flag.is_set():
        return
    _watchers[uri] = Watcher(uri, name, on_detect)
    _watchers[uri].start()


def stop_all(keep: frozenset = frozenset()) -> None:
    for uri, watcher in _watchers.items():
        if uri not in keep:
            watcher.stop()


def frigate_uri(camera: str, uris: list[str]) -> str:
    """Frigate names cameras like front_door; ours are front-door. Match them so clips line up."""
    return next(
        (uri for uri in uris if uri.replace("-", "_") == camera.lower().replace("-", "_")),
        camera,
    )


def poll_frigate(uris: list[str], since: float) -> float:
    """Save Frigate's finished events newer than `since`; returns the newest start time seen."""
    resp = requests.get(f"{frigate_url()}/api/events", params={"after": since, "limit": 100}, timeout=10)
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
    """Poll Frigate whenever an address is set; it can be set or cleared from the web UI at any time."""

    def loop():
        since = time.time() - 86400
        while True:
            try:
                if frigate_url():
                    since = poll_frigate(get_uris(), since)
            except (requests.RequestException, ValueError, KeyError) as ex:
                logger.warning(f"[DETECT] Couldn't read events from Frigate: {ex}")
            time.sleep(30)

    Thread(target=loop, daemon=True, name="frigate").start()


def frigate_media(ext_id: str, kind: str) -> requests.Response:
    """Stream a Frigate event's thumbnail.jpg or clip.mp4."""
    return requests.get(f"{frigate_url()}/api/events/{ext_id}/{kind}", stream=True, timeout=10)
