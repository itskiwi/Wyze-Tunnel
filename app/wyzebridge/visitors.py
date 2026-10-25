"""Doorbell visitor log: every ring with the picture Wyze sent, kept across restarts."""

import json
import shutil
from pathlib import Path

from wyzebridge.config import IMG_PATH, TOKEN_PATH

VISITORS_FILE = TOKEN_PATH + "visitors.json"
PIC_DIR = Path(IMG_PATH) / "visitors"
KEEP = 200


def load() -> list[dict]:
    """Newest first: [{"uri", "name", "ts", "pic"}]."""
    try:
        with open(VISITORS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return []


def add(uri: str, name: str, ts: int) -> dict:
    """Log a ring, keeping a copy of the camera's latest snapshot (Wyze's picture links expire)."""
    pic = f"{uri}_{ts}.jpg"
    try:
        PIC_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(IMG_PATH) / f"{uri}.jpg", PIC_DIR / pic)
    except OSError:
        pic = None
    visit = {"uri": uri, "name": name, "ts": ts, "pic": pic}
    log = [visit] + load()
    for old in log[KEEP:]:
        if old.get("pic"):
            (PIC_DIR / old["pic"]).unlink(missing_ok=True)
    with open(VISITORS_FILE, "w") as f:
        json.dump(log[:KEEP], f)
    return visit
