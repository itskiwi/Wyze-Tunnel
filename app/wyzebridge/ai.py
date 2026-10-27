"""Shared base for the AI models: download each once, run it on the graphics chip when there is one, else the CPU.

ONNX Runtime (MIT, github.com/microsoft/onnxruntime) runs the models. On Windows its DirectML build uses any DirectX 12
graphics chip (NVIDIA, AMD or Intel). A model that won't load or run there moves to the CPU, and if ONNX Runtime
isn't installed at all, load() returns None so callers fall back to OpenCV or switch that feature off.
AI_GPU=false keeps everything on the CPU.
"""

import os
from pathlib import Path
from threading import Lock

import requests

# No wheels for 32-bit ARM, and a broken install can fail to load its DLLs: the AI features fall back without it.
try:
    import onnxruntime as ort
except (ImportError, OSError, RuntimeError):
    ort = None

from wyzebridge.config import TOKEN_PATH
from wyzebridge.logging import logger

MODEL_DIR = Path(TOKEN_PATH, "models")
GPU = "DmlExecutionProvider"
_lock = Lock()
_models: dict[str, "Model"] = {}


def gpu_ok() -> bool:
    return bool(ort) and os.getenv("AI_GPU", "").lower() not in {"false", "no", "off", "0"} and GPU in ort.get_available_providers()


def device() -> str:
    """Where models run: "gpu", "cpu", or "" when ONNX Runtime isn't installed."""
    return "gpu" if gpu_ok() else "cpu" if ort else ""


def fetch(url: str, file: str, size: int = 0) -> Path | None:
    """Download a model file once. With a size, a file of any other size is a broken download; without one
    (a host that doesn't publish sizes), the half-written .part file is never mistaken for the model."""
    path = MODEL_DIR / file
    with _lock:
        if path.exists() and (path.stat().st_size == size or not size):
            return path
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(".part")
        logger.info(f"[AI] Downloading a model: {file}")
        try:
            with requests.get(url, timeout=60, stream=True) as resp:
                resp.raise_for_status()
                with open(part, "wb") as f:
                    for chunk in resp.iter_content(1 << 20):
                        f.write(chunk)
        except (requests.RequestException, OSError) as ex:
            logger.error(f"[AI] Couldn't download {file}: {ex}")
            part.unlink(missing_ok=True)
            return None
        if size and part.stat().st_size != size or not part.stat().st_size:
            logger.error(f"[AI] {file} downloaded {part.stat().st_size} bytes, not {size}; trying again later")
            part.unlink(missing_ok=True)
            return None
        part.replace(path)
    return path


def _session(path: Path, gpu: bool):
    opts = ort.SessionOptions()
    if gpu:  # DirectML can't run with memory patterns or in parallel
        opts.enable_mem_pattern = False
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    return ort.InferenceSession(str(path), opts, providers=[GPU, "CPUExecutionProvider"] if gpu else ["CPUExecutionProvider"])


class Model:
    """One ONNX model, shared by every camera (ONNX Runtime sessions are thread-safe)."""

    def __init__(self, path: Path):
        self.path = path
        self.session = None
        if gpu_ok():
            try:
                self.session = _session(path, True)
            except Exception as ex:  # any driver problem: the CPU always works
                logger.warning(f"[AI] {path.name} won't load on the graphics chip, using the CPU: {ex}")
        self.session = self.session or _session(path, False)
        self.input = self.session.get_inputs()[0].name

    @property
    def device(self) -> str:
        return "gpu" if GPU in self.session.get_providers() else "cpu"

    def run(self, feeds: dict) -> list:
        try:
            return self.session.run(None, feeds)
        except Exception as ex:
            if self.device != "gpu":
                raise
            logger.warning(f"[AI] {self.path.name} failed on the graphics chip, moving it to the CPU: {ex}")
            self.session = _session(self.path, False)
            return self.session.run(None, feeds)


def load(path: Path) -> Model | None:
    """The model at path, loaded once. None if ONNX Runtime is missing or the file won't load."""
    if not ort:
        return None
    with _lock:
        if str(path) not in _models:
            try:
                _models[str(path)] = Model(path)
            except Exception as ex:
                logger.error(f"[AI] Couldn't load {path.name}: {ex}")
                return None
            logger.info(f"[AI] {path.name} runs on the {'graphics chip' if _models[str(path)].device == 'gpu' else 'CPU'}")
        return _models[str(path)]
