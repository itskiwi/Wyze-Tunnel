"""Doorbell transcripts: after a ring, what was said at the door, as text. Off until the owner turns it on.

Recording a conversation can need everyone's consent where you live, so switching it on asks the owner to confirm.
whisper.cpp (MIT, github.com/ggml-org/whisper.cpp, through pywhispercpp) turns the doorbell's sound into text on this
computer. It runs in a child process, so a crash (say, a CPU too old for its build) can't take the bridge down. The
sound only ever lives in memory: just the text is kept, on the visitor entry.
"""

import importlib.util
import json
import subprocess
import sys
import time

from wyzebridge.config import TOKEN_PATH
from wyzebridge.detect import MODEL_DIR
from wyzebridge.logging import logger

SETTINGS_FILE = TOKEN_PATH + "transcripts.json"
SECONDS = 20  # how long to listen after a ring
RATE = 16000
MODEL = "base.en"  # 142 MB, downloaded once. shortcut: English only; offer "base" (any language) if people ask.
QUIET = 0.01  # RMS below this is silence: whisper makes up "Thank you." for silence, so don't ask it
# Runs in the child: reads 16 kHz s16le sound on stdin, prints the text on a marked last line.
CHILD = """
import sys
import numpy as np
from pywhispercpp.model import Model
wave = np.frombuffer(sys.stdin.buffer.read(), "<i2").astype(np.float32) / 32768
model = Model(sys.argv[1], models_dir=sys.argv[2])
print("TRANSCRIPT:" + " ".join(s.text.strip() for s in model.transcribe(wave)).strip())
"""


def available() -> bool:
    return importlib.util.find_spec("pywhispercpp") is not None


def _load() -> dict:
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def enabled() -> bool:
    return bool(_load().get("on")) and available()


def set_enabled(on, agreed=False) -> bool:
    """Raises ValueError with a message for the user."""
    if not isinstance(on, bool):
        raise ValueError("Use true or false")
    if on and agreed is not True:
        raise ValueError("Confirm that recording visitors is allowed where you live first.")
    if on and not available():
        raise ValueError("Transcripts aren't available on this device.")
    with open(SETTINGS_FILE, "w") as f:
        json.dump({"on": on, "agreed_at": int(time.time())} if on else {"on": False}, f)
    return on


def record(uri: str) -> bytes:
    """SECONDS of the camera's sound from its local stream, as 16 kHz mono s16le. Empty if it has no sound."""
    cmd = ["ffmpeg", "-loglevel", "fatal", "-rtsp_transport", "tcp", "-i", f"rtsp://127.0.0.1:8554/{uri}"]
    cmd += ["-map", "0:a:0", "-vn", "-t", str(SECONDS), "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=SECONDS + 40).stdout
    except (OSError, subprocess.TimeoutExpired):
        return b""


def loud_enough(raw: bytes) -> bool:
    import numpy as np

    wave = np.frombuffer(raw[: len(raw) // 2 * 2], "<i2").astype(np.float32) / 32768
    return wave.size > RATE and float(np.sqrt(np.mean(wave**2))) >= QUIET


def transcribe(raw: bytes) -> str:
    """Text for 16 kHz s16le sound, or "" if it fails. The first run downloads the model."""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    try:
        out = subprocess.run([sys.executable, "-c", CHILD, MODEL, str(MODEL_DIR)], input=raw, capture_output=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as ex:
        logger.warning(f"[TRANSCRIPT] {ex}")
        return ""
    lines = [line for line in out.stdout.decode(errors="replace").splitlines() if line.startswith("TRANSCRIPT:")]
    if out.returncode or not lines:
        logger.warning(f"[TRANSCRIPT] whisper.cpp failed (exit {out.returncode})")
        return ""
    return lines[-1].removeprefix("TRANSCRIPT:").strip()


def after_ring(uri: str, on_text) -> None:
    """Run in a thread after a ring: listen, transcribe, then on_text(text) if anything was said."""
    if not enabled():
        return
    raw = record(uri)
    if not loud_enough(raw):
        return
    if text := transcribe(raw):
        logger.info(f"[TRANSCRIPT] {len(text)} characters from {uri}")
        on_text(text)
