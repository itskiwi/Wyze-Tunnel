"""Search footage in plain words ("red truck", "man with a dog") and tell apart who is who, with CLIP.

CLIP ViT-B/32 by OpenAI (MIT), as the int8 ONNX export by Xenova (Apache-2.0, huggingface.co/Xenova/clip-vit-base-patch32),
turns each saved hit into two short lists of numbers: one for the whole picture (for search) and one for the spotted
person, car or animal (for Visits, the same person on another camera). Words turn into the same kind of numbers, so
the closest pictures are the best matches. Everything runs on this computer. It's on wherever built-in detection is,
downloads about 150 MB once, and if it can't load, search falls back to labels and Visits to time windows.
AI_SEARCH=false turns it off.
"""

import os
import time
from queue import Full, Queue
from threading import Lock, Thread

from wyzebridge import ai
from wyzebridge.logging import logger

try:
    import cv2
    import numpy as np
except (ImportError, RuntimeError):
    cv2 = None

try:
    from tokenizers import Tokenizer
except ImportError:
    Tokenizer = None

ON = os.getenv("AI_SEARCH", "").lower() not in {"false", "no", "off", "0"}
URL = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/"
FILES = {"clip-vision.onnx": "onnx/vision_model_quantized.onnx", "clip-text.onnx": "onnx/text_model_quantized.onnx", "clip-tokenizer.json": "tokenizer.json"}
SIZE = 224
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32) if cv2 else None
STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32) if cv2 else None
# CLIP's cosine similarity for a good word match sits around 0.25-0.3 and an unrelated picture near 0.15.
LOW, HIGH = 0.18, 0.30

_queue: Queue = Queue(maxsize=50)
_lock = Lock()
_state = {"status": "off", "failed": 0.0}  # status: off, downloading, ready, failed
RETRY = 3600  # a failed download or load is tried again an hour later
_models: dict = {}


def status() -> str:
    return _state["status"] if ON and ai.ort and cv2 and Tokenizer else "off"


def _load() -> bool:
    """Download and load the three files once; False if any is missing (tried again after RETRY)."""
    with _lock:
        if _models:
            return True
        if not (ON and ai.ort and cv2 and Tokenizer) or time.time() - _state["failed"] < RETRY:
            return False
        _state["status"] = "downloading"
        paths = {name: ai.fetch(URL + src, name) for name, src in FILES.items()}
        vision, text = ai.load(paths["clip-vision.onnx"]) if paths["clip-vision.onnx"] else None, None
        if vision and paths["clip-text.onnx"]:
            text = ai.load(paths["clip-text.onnx"])
        if not (vision and text and paths["clip-tokenizer.json"]):
            _state.update(status="failed", failed=time.time())
            return False
        _models.update(vision=vision, text=text, tok=Tokenizer.from_file(str(paths["clip-tokenizer.json"])))
        _state["status"] = "ready"
        return True


def _unit(vec):
    vec = np.asarray(vec, np.float32).reshape(-1)
    return vec / (np.linalg.norm(vec) or 1)


def _pixels(image):
    """BGR picture -> CLIP's 224x224 input: shortest side to 224, center crop, RGB, normalized."""
    h, w = image.shape[:2]
    scale = SIZE / min(h, w)
    image = cv2.resize(image, (max(SIZE, round(w * scale)), max(SIZE, round(h * scale))), interpolation=cv2.INTER_CUBIC)
    h, w = image.shape[:2]
    top, left = (h - SIZE) // 2, (w - SIZE) // 2
    rgb = image[top : top + SIZE, left : left + SIZE, ::-1].astype(np.float32) / 255
    return ((rgb - MEAN) / STD).transpose(2, 0, 1)[None]


def _run(model, feeds: dict, out: str):
    """Feed only the inputs this export takes, and pick the named output (exports differ in what they include)."""
    names = [o.name for o in model.session.get_outputs()]
    takes = {i.name for i in model.session.get_inputs()}
    result = model.run({k: v for k, v in feeds.items() if k in takes})
    return result[names.index(out)] if out in names else result[0]


def image_vec(image):
    return _unit(_run(_models["vision"], {"pixel_values": _pixels(image)}, "image_embeds"))


def text_vec(words: str):
    ids = np.array([_models["tok"].encode(words.lower()).ids[:77]], np.int64)
    return _unit(_run(_models["text"], {"input_ids": ids, "attention_mask": np.ones_like(ids)}, "text_embeds"))


def crop(frame, box, margin: float = 0.15):
    x, y, w, h = box
    dx, dy = int(w * margin), int(h * margin)
    out = frame[max(0, y - dy) : y + h + dy, max(0, x - dx) : x + w + dx]
    return out if out.size and min(out.shape[:2]) >= 8 else frame


def add(det_id: int, frame, box) -> None:
    """Embed a saved hit in the background; drops it if the queue is full (search still finds it by label)."""
    if status() == "off":
        return
    try:
        _queue.put_nowait((det_id, frame.copy(), box))
    except Full:
        pass


def _worker(db) -> None:
    while True:
        det_id, frame, box = _queue.get()
        try:
            if not _load():
                continue
            vecs = (image_vec(frame), image_vec(crop(frame, box)) if box else None)
            with db() as con:
                con.execute(
                    "INSERT OR REPLACE INTO vecs (id, frame, crop) VALUES (?, ?, ?)",
                    (det_id, vecs[0].tobytes(), vecs[1].tobytes() if vecs[1] is not None else None),
                )
        except Exception as ex:  # never let a bad picture stop the worker
            logger.warning(f"[AI] Couldn't index a hit for search: {ex}")


def start(db) -> None:
    """db() opens the detections database (with its vecs table)."""
    if status() != "off":
        Thread(target=_worker, args=(db,), daemon=True, name="clip").start()


def match(sim: float) -> float:
    """Cosine similarity -> 0..1, so the UI can show a plain "good match"."""
    return round(min(max((sim - LOW) / (HIGH - LOW), 0.0), 1.0), 2)


def rank(words: str, rows: list[dict], vecs: dict[int, bytes]) -> list[dict]:
    """Rows that match the words, best first, each with a 0..1 `match`. Rows without a vector are left out."""
    if not words.strip() or not _load():
        return []
    query = text_vec(words)
    for row in rows:
        if blob := vecs.get(row["id"]):
            row["match"] = match(float(np.frombuffer(blob, np.float32) @ query))
    return sorted((r for r in rows if r.get("match", 0) > 0), key=lambda r: -r["match"])


def same(a: bytes | None, b: bytes | None, threshold: float = 0.86) -> bool:
    """Two crops of the same person or car? shortcut: one fixed threshold, tune it with real footage."""
    if not a or not b:
        return False
    return float(np.frombuffer(a, np.float32) @ np.frombuffer(b, np.float32)) >= threshold
