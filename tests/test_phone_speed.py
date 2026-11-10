from unittest import mock

import pytest
from werkzeug.security import generate_password_hash


def test_password_check_is_remembered_only_when_right():
    from wyzebridge import auth

    hashed = generate_password_hash("right")
    with mock.patch.object(auth, "check_password_hash", wraps=auth.check_password_hash) as slow:
        assert auth.check_password(hashed, "right")
        assert auth.check_password(hashed, "right")
        assert slow.call_count == 1  # the second request skips scrypt
        assert not auth.check_password(hashed, "wrong")
        assert not auth.check_password(hashed, "wrong")
        assert slow.call_count == 3  # wrong guesses always pay full price
        assert not auth.check_password(generate_password_hash("new"), "right")  # a changed password isn't remembered


@pytest.fixture
def pictures(monkeypatch, tmp_path):
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    from flask import Flask
    from wyzebridge import config

    monkeypatch.setattr(config, "IMG_PATH", f"{tmp_path}/")
    cv2.imwrite(str(tmp_path / "porch.jpg"), np.full((1440, 2560, 3), 128, np.uint8))
    return Flask(__name__), tmp_path, cv2


PHONE = {"User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 7) Chrome/130 Mobile Safari/537.36"}


def test_phones_get_a_smaller_picture(pictures):
    app, tmp_path, cv2 = pictures
    from wyzebridge import web_ui

    with app.test_request_context("/img/porch.jpg", headers=PHONE):
        assert web_ui.phone_picture("porch.jpg") == "porch.jpg"
        assert cv2.imread(str(tmp_path / "phone" / "porch.jpg")).shape[:2] == (540, 960)
        assert web_ui.phone_picture("missing.jpg") is None
    with app.test_request_context("/img/porch.jpg?full", headers=PHONE):
        assert web_ui.phone_picture("porch.jpg") is None
    with app.test_request_context("/img/porch.jpg"):
        assert web_ui.phone_picture("porch.jpg") is None  # computers keep the full picture


def test_wrong_passwords_pause_that_address_only(monkeypatch):
    from flask import Flask
    from wyzebridge import auth

    hashed = generate_password_hash("right")
    monkeypatch.setattr(auth, "_misses", {})
    monkeypatch.setattr(auth, "_recent_ok", {})
    app, clock = Flask(__name__), [1000.0]
    monkeypatch.setattr(auth.time, "time", lambda: clock[0])

    def check(addr, password):
        with app.test_request_context(environ_base={"REMOTE_ADDR": addr}):
            return auth.check_password(hashed, password)

    assert check("192.168.1.9", "right")  # this phone is signed in before the guessing starts
    for _ in range(auth.MISSES):
        assert not check("192.168.1.7", "guess")
    with mock.patch.object(auth, "check_password_hash") as slow:
        assert not check("192.168.1.7", "right")  # paused: even the right password waits, and nothing is hashed
        slow.assert_not_called()
    assert check("192.168.1.9", "right") and check("192.168.1.8", "right")  # nobody else waits
    clock[0] += auth.PAUSE + 1
    assert check("192.168.1.7", "right")
    assert "192.168.1.7" not in auth._misses
