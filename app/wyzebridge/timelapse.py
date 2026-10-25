"""Daily timelapse: a frame every 10 minutes from the cameras picked on the Recordings page,
stitched into a short video after midnight. Sky timelapse is the same thing pointed at the sky."""

import json
import shutil
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Thread
from typing import Callable

from wyzebridge.config import IMG_PATH, TOKEN_PATH
from wyzebridge.logging import logger

SETTINGS_FILE = TOKEN_PATH + "timelapse.json"
ROOT = Path(IMG_PATH) / "timelapse"
FRAME_EVERY = 600  # 144 frames a day, 12 seconds of video at 12 fps
KEEP_DAYS = 30


def load_cams() -> list[str]:
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f).get("cams", [])
    except (FileNotFoundError, ValueError, AttributeError):
        return []


def save_cams(cams: list, uris: list[str]) -> list[str]:
    if not isinstance(cams, list):
        raise ValueError("Pick cameras from the list")
    picked = [c for c in cams if c in uris]
    with open(SETTINGS_FILE, "w") as f:
        json.dump({"cams": picked}, f)
    return picked


def add_frame(uri: str, now: datetime) -> Path:
    """Copy the camera's latest snapshot into today's folder as the next numbered frame."""
    day = ROOT / uri / now.strftime("%Y-%m-%d")
    day.mkdir(parents=True, exist_ok=True)
    frame = day / f"{len(list(day.glob('*.jpg'))):04d}.jpg"  # numbered, so ffmpeg needs no glob (Windows builds lack it)
    shutil.copyfile(Path(IMG_PATH) / f"{uri}.jpg", frame)
    return frame


def stitch(day: Path) -> bool:
    """Turn a finished day's frames into <day>.mp4 next to the folder, then delete the frames."""
    out = day.with_suffix(".mp4")
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-framerate", "12", "-i", str(day / "%04d.jpg")]
    cmd += ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)]
    try:
        ok = subprocess.run(cmd, capture_output=True, timeout=600).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if ok:
        shutil.rmtree(day, ignore_errors=True)
    else:
        logger.warning(f"[TIMELAPSE] Couldn't make {out.name} for {day.parent.name}")
    return ok


def videos() -> dict[str, list[str]]:
    """{uri: [YYYY-MM-DD, ...]} newest first."""
    if not ROOT.exists():
        return {}
    return {
        cam.name: sorted((v.stem for v in cam.glob("*.mp4")), reverse=True)
        for cam in sorted(ROOT.iterdir())
        if cam.is_dir() and any(cam.glob("*.mp4"))
    }


def video_path(uri: str, day: str) -> Path | None:
    try:
        date.fromisoformat(day)
    except ValueError:
        return None
    path = ROOT / uri / f"{day}.mp4"
    return path if path.parent.parent == ROOT and path.exists() else None


def tidy(today: date) -> None:
    """Stitch every finished day and drop videos older than KEEP_DAYS."""
    if not ROOT.exists():
        return
    oldest = (today - timedelta(days=KEEP_DAYS)).isoformat()
    for cam in ROOT.iterdir():
        for item in cam.iterdir() if cam.is_dir() else []:
            if item.is_dir() and item.name < today.isoformat():
                stitch(item)
            elif item.suffix == ".mp4" and item.stem < oldest:
                item.unlink(missing_ok=True)


def start(snap: Callable[[str], bool]) -> None:
    """snap(uri) refreshes IMG_PATH/<uri>.jpg and says whether it worked."""

    def loop():
        last: dict[str, float] = {}
        tidied = None
        while True:
            try:
                now = datetime.now()
                if tidied != now.date():
                    tidy(now.date())
                    tidied = now.date()
                for uri in load_cams():
                    if time.time() - last.get(uri, 0) >= FRAME_EVERY:
                        last[uri] = time.time()
                        if snap(uri):
                            add_frame(uri, now)
            except Exception as ex:
                logger.warning(f"[TIMELAPSE] {ex}")
            time.sleep(30)

    Thread(target=loop, name="timelapse", daemon=True).start()
