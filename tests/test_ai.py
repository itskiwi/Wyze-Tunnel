import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("TOKEN_PATH", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).parents[1] / "app"))

import pytest
from wyzebridge import ai

pytestmark = pytest.mark.skipif(ai.ort is None, reason="ONNX Runtime isn't installed")


def tiny_model(tmp_path) -> Path:
    """y = relu(x), saved by the onnx package (not a dependency, so the bytes are inlined)."""
    path = tmp_path / "tiny.onnx"
    path.write_bytes(bytes.fromhex(
        "08083a330a0c0a0178120179220452656c751201745a0f0a0178120a0a08080112040a020801620f0a0179120a0a08080112040a02080142040a00100d"
    ))
    return path


class FlakyGpu:
    """A session that claims the graphics chip and fails to run, like a driver crash."""

    def get_providers(self):
        return [ai.GPU, "CPUExecutionProvider"]

    def get_inputs(self):
        return [mock.Mock(name="x")]

    def run(self, *_):
        raise RuntimeError("device removed")


def test_gpu_failure_moves_the_model_to_the_cpu(tmp_path, monkeypatch):
    import numpy as np

    path = tiny_model(tmp_path)
    real = ai._session
    monkeypatch.setattr(ai, "gpu_ok", lambda: True)
    monkeypatch.setattr(ai, "_session", lambda p, gpu: FlakyGpu() if gpu else real(p, False))
    model = ai.Model(path)
    assert model.device == "gpu"
    assert model.run({"x": np.array([-1.0], np.float32)})[0].tolist() == [0.0]
    assert model.device == "cpu"


def test_gpu_that_wont_load_falls_back(tmp_path, monkeypatch):
    path = tiny_model(tmp_path)
    real = ai._session
    monkeypatch.setattr(ai, "gpu_ok", lambda: True)

    def session(p, gpu):
        if gpu:
            raise RuntimeError("no DirectX 12")
        return real(p, False)

    monkeypatch.setattr(ai, "_session", session)
    assert ai.load(path).device == "cpu"
    assert ai.load(tmp_path / "missing.onnx") is None  # a bad file doesn't raise


def test_short_download_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(ai, "MODEL_DIR", tmp_path)
    resp = mock.Mock(content=b"abc", raise_for_status=lambda: None)
    with mock.patch.object(ai.requests, "get", return_value=resp):
        assert ai.fetch("http://x/m.onnx", "m.onnx", 10) is None
        assert ai.fetch("http://x/m.onnx", "m.onnx", 3) == tmp_path / "m.onnx"
    assert not (tmp_path / "m.part").exists()


def test_ai_gpu_false_keeps_the_cpu(monkeypatch):
    monkeypatch.setenv("AI_GPU", "false")
    assert not ai.gpu_ok()
