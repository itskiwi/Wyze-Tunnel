from collections import deque
from unittest import mock

import numpy as np
import pytest

from wyzebridge import hearing


def look(**scores):
    out = np.zeros(521)
    for label, score in scores.items():
        out[hearing.SOUNDS[label][0][0]] = score
    return out


def test_alarm_needs_to_keep_beeping_but_glass_breaks_once():
    history = deque(maxlen=hearing.WINDOW)
    assert hearing.heard(history, look(smoke=0.9)) == []
    assert hearing.heard(history, look(smoke=0.9)) == []
    assert hearing.heard(history, look(smoke=0.9, glass=0.8)) == ["smoke", "glass"]
    for _ in range(hearing.WINDOW):
        hearing.heard(history, look(smoke=0.1))
    assert hearing.heard(history, look(smoke=0.9)) == []  # old beeps have aged out


def test_each_sound_alerts_once_per_cooldown():
    heard = mock.Mock()
    listener = hearing.Listener("kitchen", "Kitchen", heard)
    with mock.patch.object(hearing.time, "time", return_value=1000):
        listener.check(["smoke", "glass"])
        listener.check(["smoke", "glass"])
    with mock.patch.object(hearing.time, "time", return_value=1000 + hearing.COOLDOWN["glass"]):
        listener.check(["smoke", "glass"])
    assert [c.args[2] for c in heard.call_args_list] == ["smoke", "glass", "glass"]


def test_model_runs_on_yamnet_shaped_weights(tmp_path):
    """Random weights laid out like yamnet.h5: shapes line up and scores come out as 521 values in 0..1."""
    h5py = pytest.importorskip("h5py")
    rng = np.random.default_rng(0)
    path = tmp_path / "yamnet.h5"
    with h5py.File(path, "w") as f:
        def put(scope, name, shape):
            f[f"{scope}/{scope}/{name}:0"] = rng.standard_normal(shape).astype(np.float32) * 0.1

        def norm(scope, n):
            for name in ("beta", "moving_mean"):
                put(f"{scope}/bn", name, (n,))
            f[f"{scope}/bn/{scope}/bn/moving_variance:0"] = np.ones(n, np.float32)

        put("layer1/conv", "kernel", (3, 3, 1, 32))
        norm("layer1/conv", 32)
        channels = 32
        for i, out in zip(range(2, 15), (64, 128, 128, 256, 256, *(512,) * 6, 1024, 1024)):
            put(f"layer{i}/depthwise_conv", "depthwise_kernel", (3, 3, channels, 1))
            norm(f"layer{i}/depthwise_conv", channels)
            put(f"layer{i}/pointwise_conv", "kernel", (1, 1, channels, out))
            norm(f"layer{i}/pointwise_conv", out)
            channels = out
        put("logits", "kernel", (1024, 521))
        put("logits", "bias", (521,))
    scores = hearing.Yamnet(str(path)).scores(rng.standard_normal(hearing.PATCH) * 0.1)
    assert scores.shape == (521,) and ((scores >= 0) & (scores <= 1)).all()


def test_log_mel_is_one_yamnet_patch():
    assert hearing.log_mel(np.zeros(hearing.PATCH), hearing.mel_matrix()).shape == (96, 64)


def test_conv_pads_like_tensorflow():
    x = np.ones((96, 64, 4))
    assert hearing._conv3x3(x, 2, np.ones((3, 3, 4))).shape == (48, 32, 4)
    # 'same' with stride 2 on an even size pads only after, so the top-left output sees 2x2 of the input.
    assert hearing._conv3x3(x, 2, np.ones((3, 3, 4)))[0, 0, 0] == 9
    assert hearing._conv3x3(x, 1, np.ones((3, 3, 4)))[0, 0, 0] == 4


def test_settings_keep_known_cameras_only(monkeypatch, tmp_path):
    monkeypatch.setattr(hearing, "SETTINGS_FILE", str(tmp_path / "listen.json"))
    assert hearing.save_settings(["kitchen", "gone"], ["kitchen", "porch"]) == ["kitchen"]
    assert hearing.wanted("kitchen") and not hearing.wanted("porch")
    with pytest.raises(ValueError):
        hearing.save_settings("kitchen", ["kitchen"])
