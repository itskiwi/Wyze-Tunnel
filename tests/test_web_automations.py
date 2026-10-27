# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
from base64 import b64encode
from datetime import datetime, timezone
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
        self.locked, self.lock_battery, self.door_battery_low = True, 80, False

    def list_devices(self, auth, fresh=False):
        dev = lambda mac, model, type_, name, **state: {"mac": mac, "model": model, "type": type_, "name": name, "online": True, "parent": "", "state": state}  # noqa: E731
        return [dev("DOOR", "DWS2U", "ContactSensor", "Back door", open=self.door_open, low_battery=self.door_battery_low),
                dev("LOCK", "YD.LO1", "Lock", "Front lock", locked=self.locked, open=False, battery=self.lock_battery),
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


def run(now, streams=None, **settings):
    automations._write(automations.SETTINGS_FILE, automations.load() | settings)
    automations._last_poll["at"] = 0.0
    automations.tick(API, streams or {}, now)


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


LIGHTS = {"lights": {"on": True, "cameras": ["CAM1"], "devices": ["PLUG", "BULB"], "minutes": 5, "also_motion": False}}
# Mid-June at about New York: sunrise 09:25 UTC, sunset 00:30 UTC.
utc = lambda h, day=19: datetime(2026, 6, day, h, tzinfo=timezone.utc).timestamp()  # noqa: E731
NIGHT, DAY = utc(7), utc(16)


@pytest.fixture
def dark(monkeypatch, home):
    monkeypatch.setattr(automations.weather, "location", lambda: {"lat": 40.71, "lon": -74.0})


def test_sun_math(dark):
    assert automations.is_night(NIGHT) and not automations.is_night(DAY)
    assert automations.is_night(utc(9)) and not automations.is_night(utc(11))  # 5 am and 7 am in New York
    assert not automations.is_night(utc(23)) and automations.is_night(utc(1, 20))  # 7 pm and 9 pm


def test_fallback_window_without_a_location(monkeypatch, home):
    monkeypatch.setattr(automations.weather, "location", lambda: None)
    at = lambda h: datetime(2026, 6, 19, h).timestamp()  # noqa: E731
    assert automations.is_night(at(23)) and automations.is_night(at(3)) and not automations.is_night(at(12))


def test_lights_come_on_for_a_person_and_go_off_after_the_timer(dark, home):
    run(NIGHT, **LIGHTS)
    automations.on_motion(API, "CAM1", "person", NIGHT)
    assert home.on == {"PLUG": True, "BULB": True}
    automations.tick(API, {}, NIGHT + 200)
    assert home.on["PLUG"]  # not yet
    automations.on_motion(API, "CAM1", "person", NIGHT + 240)  # seen again: the timer starts over
    automations.tick(API, {}, NIGHT + 400)
    assert home.on["PLUG"]
    automations.tick(API, {}, NIGHT + 240 + 301)
    assert home.on == {"PLUG": False, "BULB": False}
    assert not automations._state().get("lights")


def test_a_light_that_was_already_on_is_left_alone(dark, home):
    home.on["BULB"] = True
    run(NIGHT, **LIGHTS)
    automations.on_motion(API, "CAM1", "person", NIGHT)
    automations.tick(API, {}, NIGHT + 1000)
    assert home.on == {"PLUG": False, "BULB": True}
    assert not [s for s in home.sent if s[0] == "BULB"]


def test_the_timer_survives_a_restart(dark, home):
    run(NIGHT, **LIGHTS)
    automations.on_motion(API, "CAM1", "person", NIGHT)
    automations.tick(API, {}, NIGHT + 301)  # a new process only has the state file
    assert home.on == {"PLUG": False, "BULB": False}


def test_lights_ignore_daytime_other_cameras_plain_motion_and_the_off_switch(dark, home):
    run(DAY, **LIGHTS)
    automations.on_motion(API, "CAM1", "person", DAY)
    automations.on_motion(API, "CAM2", "person", NIGHT)
    automations.on_motion(API, "CAM1", "motion", NIGHT)
    assert home.sent == []
    run(NIGHT, lights=LIGHTS["lights"] | {"on": False})
    automations.on_motion(API, "CAM1", "person", NIGHT)
    assert home.sent == []


def test_plain_motion_counts_when_the_setting_says_so(dark, home):
    run(NIGHT, lights=LIGHTS["lights"] | {"also_motion": True})
    automations.on_motion(API, "CAM1", "motion", NIGHT)
    assert home.on["PLUG"]


def test_lights_settings_are_validated(client):
    good = {"on": True, "cameras": ["CAM1"], "devices": ["PLUG"], "minutes": 10}
    assert client.post("/api/automations", json={"lights": good}).json["settings"]["lights"]["minutes"] == 10
    for bad in (good | {"cameras": ["CAM9"]}, good | {"devices": ["THERMO"]}, good | {"devices": []}, good | {"minutes": 99}):
        assert client.post("/api/automations", json={"lights": bad}).status_code == 400, bad


BEDTIME = {"bedtime": {"on": True, "time": "22:30", "silent": False}}
EVENING = datetime(2026, 6, 19, 22, 31).timestamp()


@pytest.fixture
def sent(monkeypatch, home):
    out = []
    monkeypatch.setattr(automations, "send_webhook", lambda *a, **kw: out.append(a) or True)
    monkeypatch.setattr(automations, "display_name", lambda cam: cam.nickname)
    monkeypatch.setattr(automations, "env_bool", lambda key, default="": "bridge.local")
    return out


def cam(name, state=0, enabled=True, substream=False):
    return SimpleNamespace(camera=SimpleNamespace(nickname=name), state=state, enabled=enabled, options=SimpleNamespace(substream=substream))


def test_bedtime_lists_only_the_problems(sent, home):
    home.locked, home.door_open, home.lock_battery, home.door_battery_low = False, True, 15, True
    streams = {"a": cam("Garage", -90), "b": cam("Porch", 3), "c": cam("Porch sub", -90, substream=True), "d": cam("Off", -90, enabled=False)}
    run(EVENING, streams, **BEDTIME)
    assert len(sent) == 1 and sent[0][:2] == ("bedtime", "all")
    msg = sent[0][2]
    assert msg.splitlines() == ["- Back door is open", "- Back door has a low battery", "- Front lock is unlocked",
                                "- Front lock has a low battery", "- Garage is offline", "Devices: http://bridge.local:5000/devices"]


def test_bedtime_all_good_says_so_or_stays_silent(sent, home):
    run(EVENING, **BEDTIME)
    automations.tick(API, {"a": cam("Porch")}, EVENING)
    run(EVENING + 86400, bedtime=BEDTIME["bedtime"] | {"silent": True})
    assert len(sent) == 1


def test_bedtime_is_sent_once_at_the_right_time(sent, home):
    run(EVENING - 120, **BEDTIME)  # 22:29
    assert sent == []
    automations.tick(API, {}, EVENING)
    automations.tick(API, {}, EVENING + 600)  # same evening, even after a restart (state file)
    assert len(sent) == 1
    run(EVENING + 3 * 3600)  # a restart at 01:31 is too late for last night's
    assert len(sent) == 1
    run(EVENING + 86400)  # the next evening
    assert len(sent) == 2


def test_bedtime_off_by_default_and_never_touches_devices(sent, home):
    run(EVENING)
    assert sent == [] and home.sent == []
    run(EVENING + 86400, **BEDTIME)
    assert home.sent == []


def test_bedtime_settings_are_validated(client):
    assert client.post("/api/automations", json={"bedtime": {"on": True, "time": "21:05"}}).json["settings"]["bedtime"] == {"on": True, "time": "21:05", "silent": False}
    for bad in ("25:00", "late", 5):
        assert client.post("/api/automations", json={"bedtime": {"on": True, "time": bad}}).status_code == 400, bad


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
