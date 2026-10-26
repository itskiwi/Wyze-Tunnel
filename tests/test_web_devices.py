# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
import json
from base64 import b64encode
from hashlib import md5
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote_plus

import pytest
from werkzeug.security import generate_password_hash
from wyzebridge import devices

AUTH = SimpleNamespace(access_token="tok", phone_id="phone")
OBJECTS = {"device_list": [
    {"product_type": "Camera", "product_model": "WYZE_CAKP2JFUS", "mac": "CAM1", "conn_state": 1},
    {"product_type": "OutdoorPlug", "product_model": "WLPPO", "mac": "PLUG", "conn_state": 1, "device_params": {"switch_state": 1}},
    {"product_type": "OutdoorPlug", "product_model": "WLPPO-SUB", "mac": "PLUG-0001", "nickname": "Porch", "conn_state": 1,
     "parent_device_mac": "PLUG", "device_params": {"switch_state": 0}},
    {"product_type": "ContactSensor", "product_model": "DWS2U", "mac": "DOOR", "conn_state": 1,
     "device_params": {"open_close_state": 1, "is_low_battery": 0}},
    {"product_type": "Lock", "product_model": "YD.LO1", "mac": "YD.LO1.abc123", "nickname": "Back Door", "conn_state": 1,
     "device_params": {"switch_state": 0}},
    {"product_type": "Thermostat", "product_model": "CO_EA1", "mac": "CO_EA1_1", "conn_state": 1},
    {"product_type": "Common", "product_model": "CO_TH1", "mac": "CO_TH1_1", "conn_state": 0},
    {"product_type": "MeshLight", "product_model": "WLPA19C", "mac": "BULB", "conn_state": 0, "device_params": {"switch_state": 1}},
]}


class Resp:
    def __init__(self, data):
        self.data, self.headers, self.request = data, {}, SimpleNamespace(method="POST", path_url="/")

    def json(self):
        return self.data

    def raise_for_status(self):
        pass


@pytest.fixture
def wyze(monkeypatch):
    """Fakes every Wyze host the devices module talks to and records the calls."""
    from wyzecam import api

    calls = []
    monkeypatch.setitem(devices._cache, "at", 0.0)
    monkeypatch.setattr(api, "API_STATS", api.Counter())

    def get(url, params=None, **kw):
        calls.append(("get", url, params))
        if url.endswith("/get_iot_prop") and params["did"] == "CO_TH1_1":
            return Resp({"code": 1, "data": {"props": {"temperature": "6749", "humidity": "46", "battery": "4"}}})
        if url.endswith("/get_iot_prop"):
            return Resp({"code": 1, "data": {"props": {"temperature": "73.3", "mode_sys": "heat", "heat_sp": "70", "cool_sp": "76"}}})
        return Resp({"ErrNo": 0, "device": {"locker_status": {"hardlock": 1, "door": 2}, "power": 80, "onoff_line": 1}})

    def post(url, json=None, data=None, **kw):
        calls.append(("post", url, json or data))
        if url.endswith("/get_object_list"):
            return Resp({"code": 1, "data": OBJECTS})
        return Resp({"code": 1, "ErrNo": 0, "data": {}})

    monkeypatch.setattr(devices, "get", get)
    monkeypatch.setattr(devices, "post", post)
    monkeypatch.setattr("wyzecam.api.post", post)
    return calls


def test_lists_devices_with_their_state(wyze):
    found = {d["mac"]: d for d in devices.list_devices(AUTH)}
    assert set(found) == {"PLUG-0001", "DOOR", "YD.LO1.abc123", "CO_EA1_1", "CO_TH1_1", "BULB"}
    assert found["PLUG-0001"]["state"] == {"on": False}
    assert found["DOOR"]["state"] == {"open": True, "low_battery": False}
    assert found["YD.LO1.abc123"]["state"] == {"locked": True, "open": False, "battery": 80, "lock_online": True}
    assert found["CO_EA1_1"]["state"]["mode_sys"] == "heat"
    assert found["CO_TH1_1"]["state"]["temperature"] == "67.49"
    assert found["BULB"]["state"] == {"on": True}  # offline bulbs skip the property read
    # Cached: a second look doesn't call Wyze again.
    before = len(wyze)
    devices.list_devices(AUTH)
    assert len(wyze) == before


def test_earth_reads_are_signed_over_the_sorted_query(wyze):
    devices.earth_props(AUTH, "CO_EA1_1", "temperature")
    _, url, params = wyze[-1]
    assert url == devices.EARTH_API + "/get_iot_prop"
    assert set(params) == {"did", "keys", "nonce"}


def test_ford_sign():
    raw = "get/openapi/lock/v1/info" + "a=1&uuid=abc" + devices.FORD_SECRET
    assert devices.ford_sign("get", "/openapi/lock/v1/info", {"uuid": "abc", "a": "1", "empty": ""}) == md5(quote_plus(raw).encode()).hexdigest()


def test_controls(wyze):
    devices.list_devices(AUTH)
    find = lambda mac: devices.find(AUTH, mac)  # noqa: E731
    assert devices.control(AUTH, find("PLUG-0001"), {"on": True}) == "turned on"
    assert {"device_mac": "PLUG-0001", "pid": "P3", "pvalue": "1"}.items() <= wyze[-1][2].items()

    devices.control(AUTH, find("BULB"), {"color": "#ff8800", "brightness": 40})
    action = wyze[-1][2]["action_list"][0]
    assert action["action_key"] == "set_mesh_property"
    assert action["action_params"]["list"][0]["plist"] == [
        {"pid": "P1501", "pvalue": "40"}, {"pid": "P1508", "pvalue": "1"}, {"pid": "P1507", "pvalue": "FF8800"}]

    devices.control(AUTH, find("CO_EA1_1"), {"mode": "cool", "cool_sp": 74})
    assert json.loads(wyze[-1][2])["props"] == {"mode_sys": "cool", "cool_sp": "74"}

    assert devices.control(AUTH, find("YD.LO1.abc123"), {"lock": False}) == "unlocked"
    _, url, form = wyze[-1]
    assert url == devices.FORD_API + "/openapi/lock/v1/control"
    assert form["uuid"] == "abc123" and form["action"] == "remoteUnlock" and form["sign"]

    for dev, bad in (("PLUG-0001", {"on": "false"}), ("CO_EA1_1", {"mode": "turbo"}), ("CO_EA1_1", {"heat_sp": "hot"}),
                     ("BULB", {"color": "red"}), ("DOOR", {"on": True}), ("YD.LO1.abc123", {"lock": "yes"})):
        with pytest.raises(ValueError):
            devices.control(AUTH, find(dev), bad)


@pytest.fixture
def client(monkeypatch, tmp_path):
    import frontend
    from wyzebridge import guests, mtx_server, web_ui

    monkeypatch.setattr(guests, "GUESTS_FILE", str(tmp_path / "guests.json"))
    monkeypatch.setattr(mtx_server, "MTX_CONFIG", str(tmp_path / "mediamtx.yml"))
    (tmp_path / "mediamtx.yml").write_text("authInternalUsers: []\n")
    for cls in {web_ui.WbAuth, frontend.WbAuth}:
        monkeypatch.setattr(cls, "enabled", True)
        monkeypatch.setattr(cls, "username", "owner")
        monkeypatch.setattr(cls, "_hashed_pass", generate_password_hash("ownerpass"))
    with mock.patch.object(frontend, "WyzeBridge") as wb:
        app = frontend.create_app()
    wb.return_value.api.auth = AUTH
    c = app.test_client()
    c.environ_base["HTTP_AUTHORIZATION"] = "Basic " + b64encode(b"owner:ownerpass").decode()
    return c


def test_unlock_is_a_logged_post(client, wyze):
    assert client.get("/api/devices").status_code == 200
    assert client.get("/devices").status_code == 200
    with mock.patch("frontend.logger") as log:
        resp = client.post("/api/devices", json={"mac": "YD.LO1.abc123", "lock": False})
    assert resp.json == {"status": "unlocked"}
    assert "owner" in log.warning.call_args[0][0] and "Back Door" in log.warning.call_args[0][0]
    assert client.post("/api/devices", json={"mac": "nope", "on": True}).status_code == 404
    assert client.post("/api/devices", json={"mac": "DOOR", "on": True}).status_code == 400
