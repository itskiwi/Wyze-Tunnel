"""Hear smoke alarms and breaking glass through the cameras' microphones, with no Wyze Cam Plus.

Runs YAMNet (Apache-2.0, github.com/tensorflow/models/tree/master/research/audioset/yamnet), Google's
521-sound AudioSet classifier, on each switched-on camera's audio. The network is a small MobileNet, so it runs in
plain NumPy from Google's own weights file (read with h5py, downloaded once): no TensorFlow and no extra runtime,
a few milliseconds of one CPU core per half second of sound.
"""

import json
import subprocess
import time
from collections import deque
from threading import Event, Lock, Thread

import requests

# Same limits as detect.py: no wheels on 32-bit ARM, and NumPy refuses very old CPUs.
try:
    import h5py
    import numpy as np
except (ImportError, RuntimeError):
    np = None

from wyzebridge.bridge_utils import env_cam
from wyzebridge.config import TOKEN_PATH
from wyzebridge.detect import MODEL_DIR
from wyzebridge.logging import logger

SETTINGS_FILE = TOKEN_PATH + "listen.json"
MODEL_URL = "https://storage.googleapis.com/audioset/yamnet.h5"
MODEL_SIZE = 15296092  # unchanged since 2019; a short file is a broken download
RATE = 16000
PATCH = 400 + 95 * 160  # 96 frames of 25 ms every 10 ms: the 0.96 s YAMNet looks at
HOP = RATE * 48 // 100  # a fresh look every 0.48 s, like YAMNet's own demo
# label -> (AudioSet classes from yamnet_class_map.csv, score, looks out of the last WINDOW that must hear it).
# An alarm keeps beeping, so it must be heard in 3 of the last 8 looks (about 4 s); glass breaks once.
# shortcut: CO alarms beep at the same pitch and come out as "smoke"; count the beeps (T3 vs T4) to tell them apart.
SOUNDS = {
    "smoke": ((393, 394), 0.25, 3),  # Smoke detector, smoke alarm; Fire alarm
    "glass": ((437,), 0.4, 1),  # Shatter. "Glass" (435) also fires on clinking dishes, so it isn't used.
}
WINDOW = 8
COOLDOWN = {"smoke": 600, "glass": 120}  # the same sound on the same camera alerts at most this often

_model_lock = Lock()
_listeners: dict[str, "Listener"] = {}


def load_settings() -> dict:
    """{"cams": [uri, ...]} saved from the Alerts page."""
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save_settings(cams, uris: list[str]) -> list[str]:
    """Raises ValueError with a message for the user."""
    if not isinstance(cams, list) or not all(isinstance(c, str) for c in cams):
        raise ValueError("Pick cameras from the list")
    if cams and np is None:
        raise ValueError("Listening needs NumPy, which isn't available on this device")
    cams = [c for c in cams if c in uris]
    with open(SETTINGS_FILE, "w") as f:
        json.dump({"cams": cams}, f)
    return cams


def wanted(uri: str) -> bool:
    """Listening on this camera: switched on on the Alerts page, or with LISTEN_<CAM>."""
    return np is not None and (uri in load_settings().get("cams", []) or bool(env_cam("listen", uri)))


def mel_matrix() -> "np.ndarray":
    """257 FFT bins -> 64 mel bands, 125-7500 Hz: the same numbers as tf.signal.linear_to_mel_weight_matrix."""
    def mel(hz):
        return 1127.0 * np.log1p(np.asarray(hz) / 700.0)

    bins = mel(np.linspace(0, RATE / 2, 257)[1:])[:, None]
    edges = np.linspace(mel(125.0), mel(7500.0), 66)
    lower, center, upper = edges[:-2], edges[1:-1], edges[2:]
    weights = np.maximum(0, np.minimum((bins - lower) / (center - lower), (upper - bins) / (upper - center)))
    return np.vstack((np.zeros((1, 64)), weights)).astype(np.float32)


def log_mel(wave, mel) -> "np.ndarray":
    """PATCH samples -> 96 x 64 log-mel patch, as YAMNet's features.py makes it."""
    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(400) / 400)  # periodic Hann, like tf.signal.stft
    frames = np.lib.stride_tricks.sliding_window_view(wave, 400)[::160] * window
    return np.log(np.abs(np.fft.rfft(frames, 512)) @ mel + 0.001)


def _conv3x3(x, stride: int, kernel):
    """3x3 'same' conv, TensorFlow style. kernel (3, 3, C) is depthwise; (3, 3, C, K) is a full conv."""
    h, w = x.shape[:2]
    oh, ow = -(-h // stride), -(-w // stride)
    ph, pw = max((oh - 1) * stride + 3 - h, 0), max((ow - 1) * stride + 3 - w, 0)
    x = np.pad(x, ((ph // 2, ph - ph // 2), (pw // 2, pw - pw // 2), (0, 0)))
    out = 0
    for dy in range(3):
        for dx in range(3):
            tap = x[dy : dy + stride * oh : stride, dx : dx + stride * ow : stride]
            out = out + (tap * kernel[dy, dx] if kernel.ndim == 3 else tap @ kernel[dy, dx])
    return out


class Yamnet:
    """YAMNet in NumPy: PATCH samples in, 521 sound scores out. Layer for layer as yamnet.py builds it."""

    STRIDES = (2, 1, 2, 1, 2, 1, 2, 1, 1, 1, 1, 1, 2, 1)  # the first plain conv, then 13 depthwise + pointwise

    def __init__(self, path: str):
        with h5py.File(path, "r") as f:
            def get(scope, name):
                return np.asarray(f[f"{scope}/{scope}/{name}:0"], np.float32)

            def norm(scope):  # batch norm without scale: center=True, scale=False, epsilon 1e-4
                bn = f"{scope}/bn"
                return get(bn, "moving_mean"), 1 / np.sqrt(get(bn, "moving_variance") + 1e-4), get(bn, "beta")

            self.layers = [(get("layer1/conv", "kernel"), norm("layer1/conv"), None, None)]
            for i in range(2, 15):
                dw, pw = f"layer{i}/depthwise_conv", f"layer{i}/pointwise_conv"
                self.layers.append((get(dw, "depthwise_kernel")[..., 0], norm(dw), get(pw, "kernel")[0, 0], norm(pw)))
            self.kernel, self.bias = get("logits", "kernel"), get("logits", "bias")
        self.mel = mel_matrix()

    def scores(self, wave) -> "np.ndarray":
        """Sound scores (0-1) for PATCH samples of 16 kHz audio scaled to -1..1."""
        x = log_mel(wave, self.mel)[..., None]
        for stride, (conv, n1, point, n2) in zip(self.STRIDES, self.layers):
            x = np.maximum((_conv3x3(x, stride, conv) - n1[0]) * n1[1] + n1[2], 0)
            if point is not None:
                x = np.maximum((x @ point - n2[0]) * n2[1] + n2[2], 0)
        return 1 / (1 + np.exp(-(x.mean((0, 1)) @ self.kernel + self.bias)))


def load_model() -> Yamnet | None:
    """Download the weights once and load them. None if the download fails."""
    path = MODEL_DIR / "yamnet.h5"
    with _model_lock:
        if not (path.exists() and path.stat().st_size == MODEL_SIZE):
            logger.info("[LISTEN] Downloading the sound model (15 MB)")
            try:
                resp = requests.get(MODEL_URL, timeout=120)
                resp.raise_for_status()
            except requests.RequestException as ex:
                logger.error(f"[LISTEN] Couldn't download the sound model: {ex}")
                return None
            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            path.with_suffix(".part").write_bytes(resp.content)
            path.with_suffix(".part").replace(path)
        return Yamnet(str(path))


def heard(history: deque, scores) -> list[str]:
    """Add one look's scores to the history and return the sounds that are now confirmed."""
    history.append({label: float(scores[list(classes)].max()) >= floor for label, (classes, floor, _) in SOUNDS.items()})
    return [label for label, (_, _, need) in SOUNDS.items() if sum(look[label] for look in history) >= need]


class Listener(Thread):
    """Read one camera's sound from its local RTSP stream and alert on alarms and breaking glass."""

    def __init__(self, uri: str, name: str, on_hear):
        super().__init__(daemon=True, name=f"listen-{uri}")
        self.uri, self.cam_name, self.on_hear = uri, name, on_hear
        self.stop_flag = Event()
        self.proc: subprocess.Popen | None = None
        self.last: dict[str, float] = {}

    def run(self):
        if not (model := load_model()):
            return
        logger.info(f"[LISTEN] Listening on {self.uri} for smoke alarms and breaking glass")
        while not self.stop_flag.is_set():
            self.proc = subprocess.Popen(self.ffmpeg_cmd(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            wave, history = np.zeros(0, np.float32), deque(maxlen=WINDOW)
            while not self.stop_flag.is_set() and len(raw := self.proc.stdout.read(HOP * 2)) == HOP * 2:
                wave = np.concatenate((wave, np.frombuffer(raw, "<i2") / 32768.0))[-PATCH:]
                if len(wave) == PATCH:
                    self.check(heard(history, model.scores(wave)))
            self.proc.kill()
            self.stop_flag.wait(10)  # the stream is down, restarting, or has no sound

    def ffmpeg_cmd(self) -> list[str]:
        return (
            ["ffmpeg", "-loglevel", "fatal", "-rtsp_transport", "tcp"]
            + ["-i", f"rtsp://127.0.0.1:8554/{self.uri}", "-map", "0:a:0", "-vn"]
            + ["-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"]
        )

    def check(self, sounds: list[str]) -> None:
        now = time.time()
        for label in sounds:
            if now - self.last.get(label, 0) >= COOLDOWN[label]:
                self.last[label] = now
                logger.info(f"[LISTEN] Heard {label} on {self.uri}")
                self.on_hear(self.uri, self.cam_name, label, now)

    def stop(self):
        self.stop_flag.set()
        if self.proc:
            self.proc.kill()


def listen(uri: str, name: str, on_hear) -> None:
    """Start listening on a camera once; calling again after a bridge restart is a no-op."""
    if uri in _listeners and not _listeners[uri].stop_flag.is_set():
        return
    _listeners[uri] = Listener(uri, name, on_hear)
    _listeners[uri].start()


def stop_all(keep: frozenset = frozenset()) -> None:
    for uri, listener in _listeners.items():
        if uri not in keep:
            listener.stop()
