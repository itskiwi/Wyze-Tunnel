# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
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
    from wyzebridge import guests, mtx_server, web_ui

    monkeypatch.setattr(guests, "GUESTS_FILE", str(tmp_path / "guests.json"))
    mtx = tmp_path / "mediamtx.yml"
    mtx.write_text(yaml.safe_dump({"authInternalUsers": [{"user": "wb", "pass": "api"}]}))
    monkeypatch.setattr(mtx_server, "MTX_CONFIG", str(mtx))
    for cls in {web_ui.WbAuth, frontend.WbAuth}:
        monkeypatch.setattr(cls, "enabled", True)
        monkeypatch.setattr(cls, "username", "owner")
        monkeypatch.setattr(cls, "_hashed_pass", generate_password_hash("ownerpass"))
        monkeypatch.setattr(cls, "api", "apikey")
    with mock.patch.object(frontend, "WyzeBridge"):
        yield frontend.create_app().test_client(), guests, mtx


def test_guest_can_watch_but_not_change(setup):
    client, guests, mtx = setup
    owner = basic("owner", "ownerpass")
    assert client.post("/guests", json={"name": "Sam", "hours": 5}, headers=owner).status_code == 400
    password = client.post("/guests", json={"name": "Sam", "hours": 24}, headers=owner).json["password"]
    users = yaml.safe_load(mtx.read_text())["authInternalUsers"]
    assert {"user": "guest", "pass": guests.stream_key(), "permissions": [{"action": "read"}]} in users

    sam = basic("Sam", password)
    page = client.get("/", headers=sam)
    assert page.status_code == 200 and b"Watching as Sam" in page.data and b"apikey" not in page.data
    signal = client.get("/signaling/cam", headers=sam).json
    assert signal["servers"][0]["credential"] == guests.stream_key()
    for path in ("/api/cam/power/off", "/account", "/settings/backup", "/cameras"):
        assert client.get(path, headers=sam).status_code == 403
    assert client.post("/guests", json={"name": "Eve", "hours": 1}, headers=sam).status_code == 403
    assert client.get("/", headers=basic("Sam", "wrong")).status_code == 401

    old_key = guests.stream_key()
    assert client.delete("/guests/Sam", headers=owner).json["status"] == "removed"
    assert client.get("/", headers=sam).status_code == 401
    assert guests.stream_key() != old_key
    assert all(u.get("user") != "guest" for u in yaml.safe_load(mtx.read_text())["authInternalUsers"])


def test_guest_login_ends_on_time(setup):
    client, guests, _ = setup
    password = guests.add_guest("Sam", 1, "owner")
    with mock.patch("wyzebridge.guests.time.time", return_value=guests._load()["guests"][0]["expires"] + 1):
        assert client.get("/", headers=basic("Sam", password)).status_code == 401
        guests.prune()
    assert guests.list_guests() == [] and guests._load()["guests"] == []


def test_family_login_has_full_access_but_not_owner_login(setup):
    client, guests, _ = setup
    password = client.post("/guests", json={"name": "Mom", "hours": 0}, headers=basic("owner", "ownerpass")).json["password"]
    mom = basic("Mom", password)
    page = client.get("/", headers=mom)
    assert b"Signed in as Mom" in page.data and b"apikey" not in page.data
    assert client.get("/cameras", headers=mom).status_code == 200
    assert client.post("/account", json={"username": "x", "password": "longenough"}, headers=mom).status_code == 403
    # The Login page hides these from family, so the requests behind them say no too.
    assert client.post("/guests", json={"name": "Eve", "hours": 0}, headers=mom).status_code == 403
    assert client.delete("/guests/Mom", headers=mom).status_code == 403
    assert client.get("/settings/backup", headers=mom).status_code == 403
    assert client.post("/settings/restore", json={"wyze_tunnel_backup": 1, "settings": {}}, headers=mom).status_code == 403
    with mock.patch("wyzebridge.guests.time.time", return_value=2**40):
        assert client.get("/cameras", headers=mom).status_code == 200  # never ends
    client.delete("/guests/Mom", headers=basic("owner", "ownerpass"))
    assert client.get("/", headers=mom).status_code == 401


def test_glance_shows_cameras_and_mode_buttons_only_to_owner(setup, monkeypatch):
    import json
    from pathlib import Path

    import frontend
    from wyzebridge import web_ui

    client, guests, _ = setup
    cams = {"front": {"nickname": "Front Door", "enabled": True, "connected": True, "img_url": "img/front.jpg", "img_time": 1},
            "yard": {"nickname": "Yard", "enabled": True, "connected": False, "status": -90}}
    monkeypatch.setattr(web_ui, "all_cams", lambda *_: {"cameras": cams})
    monkeypatch.setattr(frontend, "EVENT_LOG", [{"uri": "front", "ts": 200}, {"uri": "front", "ts": 100}])
    page = client.get("/glance", headers=basic("owner", "ownerpass")).data.decode()
    assert "Front Door" in page and "Streaming" not in page and "streaming" in page and "offline" in page
    assert 'data-ts="200"' in page and 'data-mode="away"' in page
    mom = client.post("/guests", json={"name": "Mom", "hours": 0}, headers=basic("owner", "ownerpass")).json["password"]
    page = client.get("/glance", headers=basic("Mom", mom))
    assert page.status_code == 200 and b"Yard" in page.data and b'data-mode="away"' not in page.data and b">Normal<" in page.data
    sam = client.post("/guests", json={"name": "Sam", "hours": 24}, headers=basic("owner", "ownerpass")).json["password"]
    assert client.get("/glance", headers=basic("Sam", sam)).status_code == 403

    manifest = json.loads((Path(frontend.__file__).parent / "static/manifest.webmanifest").read_text())
    assert [s["url"] for s in manifest["shortcuts"]] == ["../glance"]


def test_viewers_share_the_tunnels_camera_connection(setup, monkeypatch):
    import frontend
    from wyzebridge import config

    client, _, _ = setup
    owner = basic("owner", "ownerpass")
    wb = frontend.WyzeBridge.return_value
    wb.api.get_kvs_signal.return_value = {"result": "ok", "signalingUrl": "wss://wyze"}
    monkeypatch.setattr(config, "BRIDGE_IP", "10.0.0.21")

    # A browser asking for the camera's own WebRTC gets the tunnel's stream, so it takes no extra camera slot.
    assert "whep" in client.get("/signaling/cam?kvs", headers=owner).json
    assert b"whep" in client.get("/webrtc/cam", headers=owner).data
    wb.api.get_kvs_signal.assert_not_called()
    # go2rtc still fetches the one connection the tunnel holds.
    assert client.get("/signaling/cam?kvs&relay", headers=owner).json["signalingUrl"] == "wss://wyze"
    # A camera the tunnel isn't streaming still goes direct.
    wb.streams.get.return_value.enabled = False
    assert client.get("/signaling/cam?kvs", headers=owner).json["signalingUrl"] == "wss://wyze"


def test_cam_og_shows_busy_when_it_cannot_get_in():
    from wyzebridge.wyze_stream import StreamStatus, WyzeStream

    stream = WyzeStream.__new__(WyzeStream)
    stream.camera = mock.Mock(is_gwell=True)
    stream._state = mock.Mock(value=StreamStatus.STOPPED)
    stream.failures = 0
    assert not stream.busy
    stream.failures = 1
    assert stream.busy
    stream._state.value = StreamStatus.CONNECTED
    assert not stream.busy


def test_cam_og_waits_before_reopening_a_session(monkeypatch):
    from time import time

    from wyzebridge import wyze_stream
    from wyzebridge.wyze_stream import WyzeStream

    stream = WyzeStream.__new__(WyzeStream)
    stream.camera = mock.Mock(is_gwell=True)
    stream.uri, stream.sources, stream.source_idx = "og", ["webrtc"], 0
    stream._state = mock.Mock()
    proc = mock.patch.object(wyze_stream.mp, "Process")
    with proc as Process, mock.patch.object(wyze_stream.mp, "Queue"):
        stream.stopped_at = time()  # just dropped: the camera may still count that session
        stream._start()
        assert Process.call_args.kwargs["args"][3] > wyze_stream.SLOT_GAP - 2
        stream.stopped_at = 0
        stream._start()
        assert Process.call_args.kwargs["args"][3] == 0
