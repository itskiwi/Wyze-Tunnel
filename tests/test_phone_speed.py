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
