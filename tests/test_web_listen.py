# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
from base64 import b64encode
from unittest import mock

import pytest
from werkzeug.security import generate_password_hash


@pytest.fixture
def client(monkeypatch, tmp_path):
    import frontend
    from wyzebridge import hearing, web_ui

    monkeypatch.setattr(hearing, "SETTINGS_FILE", str(tmp_path / "listen.json"))
    for cls in {web_ui.WbAuth, frontend.WbAuth}:
        monkeypatch.setattr(cls, "enabled", True)
        monkeypatch.setattr(cls, "username", "owner")
        monkeypatch.setattr(cls, "_hashed_pass", generate_password_hash("ownerpass"))
    with mock.patch.object(frontend, "WyzeBridge") as bridge, mock.patch.object(frontend, "Thread") as thread:
        c = frontend.create_app().test_client()
        c.environ_base["HTTP_AUTHORIZATION"] = "Basic " + b64encode(b"owner:ownerpass").decode()
        yield hearing, bridge.return_value, thread, c


def test_pick_cameras_to_listen_and_restart(client):
    hearing, wb, thread, c = client
    wb.streams.streams = {"kitchen": mock.Mock(), "porch": mock.Mock()}
    resp = c.post("/alerts/listen", json={"cams": ["kitchen", "nope"]})
    assert resp.json == {"status": "saved", "cams": ["kitchen"]}
    assert hearing.load_settings() == {"cams": ["kitchen"]}
    assert thread.return_value.start.called
    assert c.post("/alerts/listen", json={"cams": "kitchen"}).status_code == 400
