# Push-to-talk signaling. Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
from base64 import b64encode
from unittest import mock

import pytest
import yaml
from werkzeug.security import generate_password_hash


def basic(user, password):
    return {"Authorization": "Basic " + b64encode(f"{user}:{password}".encode()).decode()}


@pytest.fixture
def setup(monkeypatch, tmp_path):
    import frontend
    from wyzebridge import config, guests, mtx_server, web_ui

    monkeypatch.setattr(guests, "GUESTS_FILE", str(tmp_path / "guests.json"))
    mtx = tmp_path / "mediamtx.yml"
    mtx.write_text(yaml.safe_dump({"authInternalUsers": [{"user": "wb", "pass": "api"}]}))
    monkeypatch.setattr(mtx_server, "MTX_CONFIG", str(mtx))
    monkeypatch.setattr(config, "BRIDGE_IP", "192.168.1.5")  # the tunnel streams the camera, so viewers share it
    for cls in {web_ui.WbAuth, frontend.WbAuth}:
        monkeypatch.setattr(cls, "enabled", True)
        monkeypatch.setattr(cls, "username", "owner")
        monkeypatch.setattr(cls, "_hashed_pass", generate_password_hash("ownerpass"))
        monkeypatch.setattr(cls, "api", "apikey")
    with mock.patch.object(frontend, "WyzeBridge") as bridge:
        wb = bridge.return_value
        wb.api.get_kvs_signal.return_value = {"result": "ok", "cam": "porch", "signalingUrl": "wss://kvs"}
        yield frontend.create_app().test_client(), guests, wb


def test_talk_gets_its_own_kinesis_session(setup):
    client, _, wb = setup
    owner = basic("owner", "ownerpass")
    # Watching a streamed camera shares the tunnel's connection (WHEP), which can't carry the mic back.
    assert "whep" in client.get("/signaling/porch?kvs", headers=owner).json
    wb.api.get_kvs_signal.assert_not_called()
    assert client.get("/signaling/porch?kvs&talk", headers=owner).json["signalingUrl"] == "wss://kvs"
    wb.api.get_kvs_signal.assert_called_once_with("porch")


def test_guests_cant_talk_but_family_can(setup):
    client, _, wb = setup
    owner = basic("owner", "ownerpass")
    sam = client.post("/guests", json={"name": "Sam", "hours": 24}, headers=owner).json["password"]
    resp = client.get("/signaling/porch?kvs&talk", headers=basic("Sam", sam))
    assert resp.status_code == 403 and "Guests" in resp.json["result"]
    wb.api.get_kvs_signal.assert_not_called()
    gran = client.post("/guests", json={"name": "Gran", "hours": 0}, headers=owner).json["password"]
    assert client.get("/signaling/porch?kvs&talk", headers=basic("Gran", gran)).json["result"] == "ok"
