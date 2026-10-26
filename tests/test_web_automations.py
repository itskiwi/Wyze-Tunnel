# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
from base64 import b64encode
from types import SimpleNamespace
from unittest import mock

import pytest
from werkzeug.security import generate_password_hash
from wyzebridge import automations, devices

API = SimpleNamespace(auth=SimpleNamespace(), refresh_token=lambda: None)


class FakeHome:
    """Stands in for the Wyze cloud: a thermostat, a door sensor, and a plug and bulb. Records every command."""

    def __init__(self):
        self.door_open = False
        self.mode = "heat"
        self.on = {"PLUG": False, "BULB": False}
        self.sent = []

    def list_devices(self, auth, fresh=False):
        dev = lambda mac, model, type_, name, **state: {"mac": mac, "model": model, "type": type_, "name": name, "online": True, "parent": "", "state": state}  # noqa: E731
        return [dev("DOOR", "DWS2U", "ContactSensor", "Back door", open=self.door_open, low_battery=False),
                dev("THERMO", "CO_EA1", "Thermostat", "Hall", mode_sys=self.mode),
                dev("PLUG", "WLPP1", "Plug", "Porch plug", on=self.on["PLUG"]),
                dev("BULB", "WLPA19C", "MeshLight", "Garden", on=self.on["BULB"])]

    def control(self, auth, dev, data):
        self.sent.append((dev["mac"], data))
        if "mode" in data:
            self.mode = data["mode"]
        if "on" in data:
            self.on[dev["mac"]] = data["on"]
        return "ok"


@pytest.fixture
def home(monkeypatch, tmp_path):
    fake = FakeHome()
    monkeypatch.setattr(devices, "list_devices", fake.list_devices)
    monkeypatch.setattr(devices, "control", fake.control)
    monkeypatch.setattr(automations, "SETTINGS_FILE", str(tmp_path / "automations.json"))
    monkeypatch.setattr(automations, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(automations, "_open_since", {})
    monkeypatch.setitem(automations._last_poll, "at", 0.0)
    return fake


def run(now, **settings):
    automations._write(automations.SETTINGS_FILE, automations.load() | settings)
    automations._last_poll["at"] = 0.0
    automations.tick(API, {}, now)


HEAT = {"heat": {"on": True, "sensors": ["DOOR"], "minutes": 2}}


def test_open_door_pauses_heat_then_restores_it(home):
    run(1000, **HEAT)
    home.door_open = True
    run(1060, **HEAT)  # just opened
    assert home.sent == []
    run(1200, **HEAT)  # open for over 2 minutes
    assert home.sent == [("THERMO", {"mode": "off"})]
    assert automations._state()["heat"]["mode"] == "heat"
    run(1300, **HEAT)  # still open: nothing more
    assert len(home.sent) == 1
    home.door_open = False
    run(1400, **HEAT)
    assert home.sent[-1] == ("THERMO", {"mode": "heat"}) and home.mode == "heat"
    assert "heat" not in automations._state()


def test_a_short_open_door_does_nothing(home):
    home.door_open = True
    run(1000, **HEAT)
    home.door_open = False
    run(1060, **HEAT)
    home.door_open = True
    run(1100, **HEAT)  # the timer started over
    run(1150, **HEAT)
    assert home.sent == []


def test_restart_during_the_pause_still_restores(home):
    home.door_open = True
    run(1000, **HEAT)
    run(1200, **HEAT)
    assert home.mode == "off"
    automations._open_since.clear()  # the bridge restarted: only the state file is left
    home.door_open = False
    run(1300, **HEAT)
    assert home.mode == "heat"


def test_a_manual_change_during_the_pause_is_left_alone(home):
    home.door_open = True
    run(1000, **HEAT)
    run(1200, **HEAT)
    home.mode = "cool"  # someone used the thermostat
    home.door_open = False
    run(1300, **HEAT)
    assert home.mode == "cool" and len(home.sent) == 1
    assert "heat" not in automations._state()


def test_turning_it_off_mid_pause_restores_and_off_by_default(home):
    run(1000)  # no settings at all
    home.door_open = True
    run(1100)
    run(1300)
    assert home.sent == []
    run(1400, **HEAT)
    run(1600, **HEAT)
    assert home.mode == "off"
    run(1700, heat={"on": False, "sensors": ["DOOR"], "minutes": 2})
    assert home.mode == "heat"


def test_a_thermostat_that_is_already_off_isnt_paused(home):
    home.mode = "off"
    home.door_open = True
    run(1000, **HEAT)
    run(1200, **HEAT)
    assert home.sent == [] and "heat" not in automations._state()


@pytest.fixture
def client(monkeypatch, home, tmp_path):
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
    wb.return_value.api.auth = API.auth
    wb.return_value.api.get_cameras.return_value = [SimpleNamespace(mac="CAM1", nickname="Front", name_uri="front", model_name="Cam")]
    monkeypatch.setattr(frontend.wyze_api, "display_name", lambda cam: cam.nickname)
    c = app.test_client()
    c.environ_base["HTTP_AUTHORIZATION"] = "Basic " + b64encode(b"owner:ownerpass").decode()
    return c


def test_settings_are_validated(client):
    ok = client.post("/api/automations", json={"heat": {"on": True, "sensors": ["DOOR"], "minutes": "5"}})
    assert ok.json["settings"]["heat"] == {"on": True, "sensors": ["DOOR"], "minutes": 5}
    assert client.get("/api/automations").json["settings"]["heat"]["minutes"] == 5
    assert client.get("/api/automations").json["cameras"] == [{"mac": "CAM1", "name": "Front"}]
    for bad in ({"heat": {"on": True, "sensors": ["NOPE"]}},  # unknown mac
                {"heat": {"on": True, "sensors": ["PLUG"]}},  # not a contact sensor
                {"heat": {"on": True, "sensors": []}},
                {"heat": {"on": True, "sensors": ["DOOR"], "minutes": 0}},
                {"heat": {"on": True, "sensors": ["DOOR"], "minutes": 61}},
                {"heat": {"on": True, "sensors": ["DOOR"], "minutes": True}},
                {"heat": {"on": True, "sensors": "DOOR"}},
                {"heat": "on"}, {"other": {}}, {}):
        assert client.post("/api/automations", json=bad).status_code == 400, bad


def test_only_the_owner_changes_automations(client, monkeypatch):
    import frontend
    from wyzebridge import web_ui

    for cls in {web_ui.WbAuth, frontend.WbAuth}:
        monkeypatch.setattr(cls, "api", "apikey")
    gran = client.post("/guests", json={"name": "Gran", "hours": 0}).json["password"]
    family = {"Authorization": "Basic " + b64encode(f"Gran:{gran}".encode()).decode()}
    body = {"heat": {"on": True, "sensors": ["DOOR"]}}
    assert client.post("/api/automations", json=body, headers=family).status_code == 403
    assert client.get("/api/automations", headers=family).status_code == 200
    assert automations.load() == {}
