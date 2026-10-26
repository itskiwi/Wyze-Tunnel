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


def test_only_the_owners_password_unlocks(client, wyze, monkeypatch):
    import frontend
    from wyzebridge import web_ui

    for cls in {web_ui.WbAuth, frontend.WbAuth}:
        monkeypatch.setattr(cls, "api", "apikey")
    gran = client.post("/guests", json={"name": "Gran", "hours": 0}).json["password"]
    family = {"Authorization": "Basic " + b64encode(f"Gran:{gran}".encode()).decode()}
    unlock = {"mac": "YD.LO1.abc123", "lock": False}
    assert client.post("/api/devices", json=unlock, headers=family).status_code == 403
    assert client.post("/api/devices?api=apikey", json=unlock, headers={"Authorization": ""}).status_code == 403
    # The API key with the owner's name but a wrong password is still not the owner.
    wrong = {"Authorization": "Basic " + b64encode(b"owner:guess").decode()}
    assert client.post("/api/devices?api=apikey", json=unlock, headers=wrong).status_code == 403
    assert not [c for c in wyze if "control" in c[1]]  # nothing reached Wyze
    assert client.post("/api/devices", json={"mac": "YD.LO1.abc123", "lock": True}).json == {"status": "locked"}


CODE_DIGITS = "482913"
LOG = {"ErrNo": 0, "family_record": [
    {"eventid": 2216, "time": 1790000060000, "uuid": "abc123", "detail": {"source": 2, "source_name": "", "email": "me@example.com", "avatar": "http://x/a.png"}},
    {"eventid": 2203, "time": 1790000000000, "detail": {"source": 1, "source_name": "Sam", "userid": "u1"}},
    {"eventid": 2214, "time": 1789999000000, "detail": {}},
    {"eventid": 2218, "time": 1789998000000, "detail": {"left_open_time": 3}},
    {"eventid": 2228, "time": 1789997000000},
    {"eventid": 2299, "time": 1789996000000, "detail": {"source": 99}},
]}
PWDS = {"ErrNo": 0, "passwords": [
    {"id": 1, "name": "Cleaner", "pwdValue": CODE_DIGITS, "password": CODE_DIGITS, "pwd_state": 1, "permission": {"status": 4, "begin": 1, "end": 2}},
    {"id": 2, "name": "Family", "pwdValue": CODE_DIGITS, "pwd_state": 1, "permission": {"status": 1}},
    {"id": 3, "name": "Weekdays", "pwd": CODE_DIGITS, "pwd_state": 4, "period_info": {"type": 1, "begin": "09:00", "end": "17:00", "valid_days": [1, 2]}},
]}


@pytest.fixture
def lock_api(wyze, monkeypatch):
    seen = []
    base = devices.get

    def get(url, params=None, **kw):
        seen.append((url, params))
        if url.endswith("/safety/family_record"):
            return Resp(LOG)
        if url.endswith("/lock/v1/pwd"):
            return Resp(PWDS)
        return base(url, params, **kw)

    monkeypatch.setattr(devices, "get", get)
    monkeypatch.setattr(devices, "_lock_cache", {})
    return seen


def test_lock_log_parses_and_never_returns_code_digits(lock_api):
    out = devices.lock_log(AUTH, "YD.LO1.abc123")
    assert [e["event"] for e in out["events"]] == ["Locked", "Unlocked", "Opened", "Open more than 5 min",
                                                   "Multiple failed fingerprint unlock attempts", "Event 2299"]
    assert out["events"][0]["source"] == "Keypad" and out["events"][2]["source"] is None and out["events"][5]["source"] is None
    assert out["events"][1] == {"time": 1790000000, "event": "Unlocked", "who": "Sam", "source": "App"}
    assert devices._lock_event({"eventid": 2203, "detail": {"source": 77}})["source"] == "source 77"
    assert [(c["name"], c["type"], c["expired"]) for c in out["codes"]] == [
        ("Cleaner", "temporary", False), ("Family", "permanent", False), ("Weekdays", "recurring", True)]
    assert out["codes"][2]["days"] == [1, 2] and out["codes"][2]["from"] == "09:00"
    assert CODE_DIGITS not in json.dumps(out) and "me@example.com" not in json.dumps(out)
    assert {u.rsplit("/", 1)[-1] for u, _ in lock_api} == {"family_record", "pwd"}  # reads only
    devices.lock_log(AUTH, "YD.LO1.abc123")
    assert len(lock_api) == 2  # cached


def test_lock_log_endpoint_strips_digits(client, lock_api):
    resp = client.get("/api/devices/lock-log?mac=YD.LO1.abc123")
    assert resp.status_code == 200 and CODE_DIGITS not in resp.get_data(as_text=True)
    assert client.get("/api/devices/lock-log?mac=DOOR").status_code == 404


def test_guests_cannot_read_history_or_change_the_alert(client, lock_api):
    gran = client.post("/guests", json={"name": "Gran", "hours": 0}).json["password"]
    visitor = client.post("/guests", json={"name": "Vi", "hours": 24}).json["password"]
    auth = lambda n, p: {"Authorization": "Basic " + b64encode(f"{n}:{p}".encode()).decode()}  # noqa: E731
    for path in ("/api/devices/lock-log?mac=YD.LO1.abc123", "/api/devices/history?mac=CO_EA1_1", "/api/devices", "/devices"):
        assert client.get(path, headers=auth("Vi", visitor)).status_code == 403
    assert client.post("/api/devices/lock-alert", json={"minutes": 5}, headers=auth("Vi", visitor)).status_code == 403
    assert client.post("/api/devices/lock-alert", json={"minutes": 5}, headers=auth("Gran", gran)).status_code == 403
    assert client.get("/api/devices/lock-log?mac=YD.LO1.abc123", headers=auth("Gran", gran)).status_code == 403


def test_lock_alert_setting_is_validated(client, monkeypatch, tmp_path):
    from wyzebridge import webhooks

    monkeypatch.setattr(webhooks, "ALERTS_FILE", str(tmp_path / "alerts.json"))
    monkeypatch.setattr(webhooks, "ALERTS", {})
    assert client.post("/api/devices/lock-alert", json={"minutes": 15}).json == {"lock_minutes": 15}
    assert webhooks.ALERTS["lock_minutes"] == 15
    webhooks.save_alerts({"topic": "abc"})  # saving the Alerts page keeps it
    assert webhooks.ALERTS["lock_minutes"] == 15
    for bad in ("x", -1, 5000, True, None):
        assert client.post("/api/devices/lock-alert", json={"minutes": bad}).status_code == 400
    assert client.post("/api/devices/lock-alert", json={"minutes": 0}).json == {"lock_minutes": 0}


def test_unlocked_alert_fires_once_after_n_minutes_and_resets_on_lock(monkeypatch):
    sent = []
    monkeypatch.setattr(devices, "ALERTS", {"lock_minutes": 10})
    monkeypatch.setattr(devices, "send_webhook", lambda *a, **k: sent.append(a) or True)
    monkeypatch.setattr(devices, "_unlocked", {})
    monkeypatch.setattr(devices, "_alerted", set())
    lock = lambda locked: [{"type": "Lock", "mac": "L", "name": "Back Door", "state": {"locked": locked}}]  # noqa: E731
    devices.lock_alert(lock(False), 1000)
    devices.lock_alert(lock(False), 1000 + 9 * 60)
    assert not sent
    devices.lock_alert(lock(False), 1000 + 10 * 60)
    devices.lock_alert(lock(False), 1000 + 60 * 60)
    assert len(sent) == 1 and sent[0][0] == "lock" and sent[0][1] == "Back Door"
    devices.lock_alert([{"type": "Lock", "mac": "L", "name": "Back Door", "state": {"error": "x"}}], 5000)  # a failed read changes nothing
    devices.lock_alert(lock(True), 5000)
    devices.lock_alert(lock(False), 6000)
    devices.lock_alert(lock(False), 6000 + 10 * 60)
    assert len(sent) == 2
    monkeypatch.setattr(devices, "ALERTS", {})  # off by default
    devices.lock_alert(lock(False), 99999)
    assert len(sent) == 2


def test_lock_alert_event_is_always_allowed(monkeypatch):
    from wyzebridge import webhooks

    monkeypatch.setattr(webhooks, "ALERTS", {"server": "https://ntfy.sh", "topic": "abc"})
    assert webhooks.alert_url("lock") == "https://ntfy.sh/abc"


def test_history_is_kept_and_summed(monkeypatch, tmp_path):
    monkeypatch.setattr(devices, "HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr(devices, "_history", {})
    dev = lambda ws: {"mac": "T", "model": "CO_EA1", "state": {"temperature": "73.3", "humidity": "40", "heat_sp": "70", "cool_sp": "76", "working_state": ws}}  # noqa: E731
    now = 1_000_000
    for i, ws in enumerate(("heating", "heating", "idle", "cooling")):
        devices.record_history([dev(ws)], now + i * 300)
    devices.record_history([dev("idle")], now + 3 * 300 + 60)  # too soon: skipped
    devices.record_history([{"mac": "T", "model": "CO_EA1", "state": {"error": "x"}}], now + 99999)  # failed reads aren't recorded
    out = devices.history("T", 24, now + 4 * 300)
    assert len(out["points"]) == 4 and out["points"][0] == {"t": now, "temp": 73.3, "hum": 40.0, "heat": 70.0, "cool": 76.0, "run": "heat"}
    assert out["heat_minutes"] == 10 and out["cool_minutes"] == 5
    assert devices.history("T", 24, now + 3 * 86400)["points"] == []
    assert json.load(open(tmp_path / "history.json"))["T"][0]["t"] == now
    assert devices.history("nope", 24)["points"] == []


def test_history_endpoint(client, monkeypatch):
    monkeypatch.setattr(devices, "_history", {})
    assert client.get("/api/devices/history?mac=CO_EA1_1&hours=24").json["points"] == []
    assert client.get("/api/devices/history?mac=CO_EA1_1&hours=5").status_code == 400


def test_plug_usage_reads_the_apps_usage_record(wyze, monkeypatch):
    seen = []

    def post(url, json=None, **kw):
        seen.append((url, json))
        rows = [{"date_ts": 1790100000000, "data": "[0,5,99,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,30]"}, {"date_ts": 1790000000000, "data": "oops"}, {"data": "[1]"}]
        return Resp({"code": 1, "data": {"usage_record_list": rows}})

    monkeypatch.setattr(devices, "post", post)
    monkeypatch.setattr(devices, "_usage_cache", {})
    sub = {"mac": "PLUG-0001", "parent": "PLUG"}
    out = devices.plug_usage(AUTH, sub, now=1790000000)
    assert [(d["date_ts"], sum(d["minutes"])) for d in out] == [(1790100000000, 95)]  # 99 is clamped to 60; bad rows are skipped
    url, body = seen[0]
    assert url.endswith("/v2/plug/usage_record_list") and body["device_mac"] == "PLUG" and body["sv"] == "56ed575478344d019f7143971ca6ca86"
    assert body["date_end"] - body["date_begin"] == 777600000 and body["phone_system_type"] == 2
    devices.plug_usage(AUTH, sub)
    assert len(seen) == 1  # cached


def test_plug_usage_endpoint_is_for_plugs_only(client, wyze, monkeypatch):
    monkeypatch.setattr(devices, "plug_usage", lambda auth, dev, fresh=False: [{"date_ts": 1, "minutes": [1]}])
    assert client.get("/api/devices/usage?mac=PLUG-0001").json == {"days": [{"date_ts": 1, "minutes": [1]}]}
    assert client.get("/api/devices/usage?mac=YD.LO1.abc123").status_code == 404
