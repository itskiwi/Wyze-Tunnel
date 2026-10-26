"""Overnight copy of the last day on each opted-in camera's SD card into files, one camera and one clip at a time."""

import json
import time
from datetime import datetime
from pathlib import Path
from threading import Thread

from wyzebridge import sd_playback
from wyzebridge.config import TOKEN_PATH
from wyzebridge.logging import logger
from wyzebridge.wyze_stream import WyzeStream

SETTINGS_FILE = TOKEN_PATH + "sd_copy.json"
# shortcut: kept beside the tokens, not under the recordings folder, so the clip trimmer and cloud backup leave them alone.
ROOT = Path(TOKEN_PATH) / "sd_copy"
CHUNK = 600  # seconds a clip covers; keeps one replay short
DEFAULTS = {"cams": [], "from": "01:00", "to": "05:00", "max_gb": 20}
status = {"state": "off", "message": "", "last": 0, "copied": 0}
_done: dict[str, str] = {}  # camera: the date it was last fully copied, so a night is one pass


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE) as f:
            return DEFAULTS | json.load(f)
    except (FileNotFoundError, ValueError):
        return dict(DEFAULTS)


def save_settings(form: dict, uris: list[str]) -> dict:
    """Validate and save. Raises ValueError with a message for the user."""
    cams = form.get("cams") or []
    if not isinstance(cams, list) or not all(isinstance(c, str) for c in cams):
        raise ValueError("Pick cameras from the list")
    try:
        times = [_minutes(form.get(k)) for k in ("from", "to")]
        max_gb = int(form.get("max_gb"))
    except (TypeError, ValueError):
        raise ValueError("Use times like 01:00 and a size in GB") from None
    if not 1 <= max_gb <= 1000:
        raise ValueError("Pick a size between 1 and 1000 GB")
    settings = {"cams": [c for c in cams if c in uris], "from": form["from"], "to": form["to"], "max_gb": max_gb}
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f)
    return settings


def _minutes(hhmm) -> int:
    t = datetime.strptime(hhmm, "%H:%M")
    return t.hour * 60 + t.minute


def in_window(settings: dict, now: datetime) -> bool:
    start, end, cur = _minutes(settings["from"]), _minutes(settings["to"]), now.hour * 60 + now.minute
    return start <= cur < end if start <= end else cur >= start or cur < end  # a window may run past midnight


def copied_files(cam: str = "") -> list[Path]:
    return sorted(ROOT.joinpath(cam).rglob("*.mp4"), key=lambda f: f.stat().st_mtime)


def prune(max_gb: int) -> None:
    """Delete the oldest copies until the total is under the cap."""
    files = copied_files()
    total = sum(f.stat().st_size for f in files)
    while files and total > max_gb * 1024**3:
        total -= files[0].stat().st_size
        files.pop(0).unlink(missing_ok=True)


def _chunks(streams, cam: str, now: float) -> list[tuple[int, int]]:
    start = int(now) - 86400
    chunks = []
    for half in (start, start + 43200):
        value = streams.send_cmd(cam, "sd_minutes", half).get("value")
        if not isinstance(value, dict):
            raise ValueError("the camera didn't answer")
        for seg_start, seg_end in value["segments"]:
            chunks += [(t, min(t + CHUNK, seg_end)) for t in range(seg_start, seg_end, CHUNK)]
    return chunks


def _watched(cam: str) -> bool:
    return bool((p := sd_playback.players.get(f"{cam}-sd")) and p[0].is_alive())


def copy_cam(streams, cam: str, settings: dict, now: float) -> bool:
    """Copy the camera's last day. False if it stopped early (window over, someone watching, camera trouble)."""
    stream = streams.get(cam)
    if not isinstance(stream, WyzeStream) or stream.source != "tutk" or not stream.connected or _watched(cam):
        return False
    try:
        chunks = _chunks(streams, cam, now)
    except ValueError as ex:
        status["message"] = f"{cam}: {ex}"
        return False
    out_dir = ROOT / cam
    out_dir.mkdir(parents=True, exist_ok=True)
    for start, end in chunks:
        out = out_dir / f"{start}.mp4"
        if out.exists():
            continue
        if not in_window(settings, datetime.now()) or _watched(cam):
            return False
        status.update(state="copying", message=f"{cam}: {datetime.fromtimestamp(start):%b %d %H:%M}")
        if sd_playback.save(cam, stream.user, stream.camera, stream.options, start, end, str(out)):
            status["copied"] += 1
            prune(settings["max_gb"])
        else:
            status["message"] = f"{cam}: the camera stopped sending, will try again"
            return False
    return True


def run(streams, now: float | None = None) -> None:
    settings = load_settings()
    now = now or time.time()
    today = f"{datetime.fromtimestamp(now):%Y-%m-%d}"
    if not settings["cams"]:
        status.update(state="off", message="")
    elif not in_window(settings, datetime.fromtimestamp(now)):
        status.update(state="waiting", message=f"Next copy at {settings['from']}")
    else:
        for cam in settings["cams"]:
            if _done.get(cam) != today and copy_cam(streams, cam, settings, now):
                _done[cam] = today
            if not in_window(settings, datetime.now()):
                break
        status.update(state="waiting", last=int(time.time()))


def start(streams) -> None:
    def loop():
        while True:
            try:
                run(streams)
            except Exception as ex:  # never let the night loop die
                logger.warning(f"[SD COPY] {type(ex).__name__}: {ex}")
            time.sleep(300)

    Thread(target=loop, name="sd-copy", daemon=True).start()


def busy(cam: str) -> bool:
    return status["state"] == "copying" and status["message"].startswith(f"{cam}:")
