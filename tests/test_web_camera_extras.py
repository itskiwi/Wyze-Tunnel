# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
import json
from base64 import b64encode
from types import SimpleNamespace
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
    with mock.patch.object(frontend, "WyzeBridge") as wb:
        app = frontend.create_app()
    streams = wb.return_value.streams
    streams.get.return_value = mock.Mock()
    streams.send_cmd.return_value = {"status": "success"}
    yield app.test_client(), guests, streams


def test_web_new_write_commands_are_owner_only(setup):
    client, guests, streams = setup
    owner = basic("owner", "ownerpass")
    watch = basic("Sam", guests.add_guest("Sam", 24, "owner"))
    family = basic("Mom", guests.add_guest("Mom", 0, "owner"))
    writes = ["wdr/on", "fl_motion/off", "fl_duration/60", "fl_brightness/50"]
    for who in (watch, family):
        for path in writes:
            assert client.get(f"/api/front/{path}", headers=who).status_code == 403
        assert client.post("/api/v1/cameras/front/commands/wdr", json={"value": "on"}, headers=who).status_code == 403
    assert streams.send_cmd.call_count == 0
    for path in writes:
        assert client.get(f"/api/front/{path}", headers=owner).status_code == 200
    assert client.post("/api/v1/cameras/front/commands/wdr", json={"value": "off"}, headers=owner).status_code == 200
    # Reading is open to anyone who can use the API (a watch-only guest still can't reach /api).
    assert client.get("/api/front/wdr", headers=family).status_code == 200
    assert client.get("/api/front/wdr", headers=watch).status_code == 403
    assert streams.send_cmd.call_args_list[0].args == ("front", "wdr", "on")


def test_web_protocol_bytes():
    from wyzecam.tutk import tutk_protocol as p

    assert p.K12092SetDb2WDRStatus(2).encode()[-1:] == b"\x02"
    assert p.K12090GetDb2WDRStatus().parse_response(b"\x01") == 1
    assert p.K12084GetDb2BatteryLevel().parse_response(b"\x55") == 85
    body = p.K10790SetIntegratedFloodlightInfo({"brightness": "40"}).encode()[16:]
    assert json.loads(body) == {"floodlightInfo": {"brightness": "40", "protocolVersion": "1"}}
    assert p.K10788GetIntegratedFloodlightInfo().parse_response(b'{"floodlightInfo": {"brightness": 7}}') == {"brightness": 7}
    assert p.K10790SetIntegratedFloodlightInfo({}).code == 10790


def fake_session(current):
    sent = []

    class Mux:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def send_ioctl(self, msg):
            sent.append(msg)
            return SimpleNamespace(result=lambda timeout=None: current if msg.code == 10788 else b"\x01")

    return SimpleNamespace(iotctrl_mux=Mux), sent


def test_web_floodlight_v2_changes_one_setting_and_keeps_the_rest():
    from wyzebridge import wyze_control

    motion = {"model": "1", "dispose": [1, 2], "durations": "30", "schedule": {"starttime": {}}, "junk": 1}
    sess, sent = fake_session({"motion": motion, "motionSwitch": "1", "brightness": 50})
    assert wyze_control.floodlight_v2_set(sess, "fl_duration", "120")["status"] == "success"
    assert sent[-1].info == {"motion": {"model": "1", "dispose": [1, 2], "durations": "120", "schedule": {"starttime": {}}}, "motionSwitch": "1"}
    assert wyze_control.floodlight_v2_set(sess, "fl_motion", "off")["status"] == "success"
    assert sent[-1].info["motionSwitch"] == "2" and sent[-1].info["motion"]["durations"] == "30"
    sent.clear()
    assert wyze_control.floodlight_v2_set(sess, "fl_brightness", "80")["status"] == "success"
    assert [m.code for m in sent] == [10790] and sent[0].info == {"brightness": "80"}
    for topic, bad in (("fl_brightness", "0"), ("fl_brightness", "101"), ("fl_duration", "x"), ("fl_motion", "maybe")):
        assert wyze_control.floodlight_v2_set(sess, topic, bad)["status"] == "error"
    assert len(sent) == 1  # bad values never reach the camera
