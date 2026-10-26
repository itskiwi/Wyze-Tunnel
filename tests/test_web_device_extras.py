# Sorts after test_web_devices.py; reuses its fakes.
from base64 import b64encode
from datetime import datetime

import pytest
from test_web_devices import AUTH, client, wyze  # noqa: F401
from wyzebridge import devices

DAY = 86400


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(devices, "SCHEDULE_FILE", str(tmp_path / "sched.json"))
    monkeypatch.setattr(devices, "HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr(devices, "_history", {})
    monkeypatch.setattr(devices, "_ran", set())


def basic(n, p):
    return {"Authorization": "Basic " + b64encode(f"{n}:{p}".encode()).decode()}


def test_owner_saves_and_reads_a_schedule(client, wyze, store):
    entry = {"time": "07:30", "on": True, "days": [3, 1, 1]}
    out = client.post("/api/devices/schedules", json={"mac": "PLUG-0001", "schedules": [entry]})
    assert out.status_code == 200 and out.json[0]["days"] == [1, 3] and out.json[0]["next"] > 0
    assert client.get("/api/devices/schedules").json["PLUG-0001"][0]["time"] == "07:30"
    assert client.post("/api/devices/schedules", json={"mac": "PLUG-0001", "schedules": []}).json == []
    assert client.get("/api/devices/schedules").json == {}


def test_schedule_input_is_validated_and_locks_refused(client, wyze, store):
    ok = {"time": "07:30", "on": True, "days": [1]}
    for bad in ([{**ok, "time": "7:30"}], [{**ok, "time": "24:00"}], [{**ok, "on": "yes"}], [{**ok, "days": []}], [{**ok, "days": [7]}],
                [{**ok, "days": [True]}], [ok] * 11, "x"):
        assert client.post("/api/devices/schedules", json={"mac": "PLUG-0001", "schedules": bad}).status_code == 400
    assert client.post("/api/devices/schedules", json={"mac": "YD.LO1.abc123", "schedules": [ok]}).status_code == 400
    assert client.post("/api/devices/schedules", json={"mac": "nope", "schedules": [ok]}).status_code == 404
    assert devices.load_schedules() == {}


def test_guests_and_family(client, wyze, store):
    gran = client.post("/guests", json={"name": "Gran", "hours": 0}).json["password"]
    vi = client.post("/guests", json={"name": "Vi", "hours": 24}).json["password"]
    body = {"mac": "PLUG-0001", "schedules": [{"time": "07:30", "on": True, "days": [1]}]}
    assert client.post("/api/devices/schedules", json=body, headers=basic("Vi", vi)).status_code == 403
    assert client.get("/api/devices/schedules", headers=basic("Vi", vi)).status_code == 403
    assert client.post("/api/devices/schedules", json=body, headers=basic("Gran", gran)).status_code == 403
    assert client.get("/api/devices/schedules", headers=basic("Gran", gran)).status_code == 200
    assert devices.load_schedules() == {}


def test_scheduler_fires_once_in_its_minute_on_its_days(client, wyze, store):
    client.post("/api/devices/schedules", json={"mac": "PLUG-0001", "schedules": [{"time": "07:30", "on": True, "days": [1]}]})
    wyze.clear()
    mon = datetime(2026, 10, 26, 7, 30, 5)  # a Monday
    devices.run_due(AUTH, datetime(2026, 10, 27, 7, 30))  # Tuesday: not a listed day
    devices.run_due(AUTH, datetime(2026, 10, 26, 7, 29))
    assert not wyze
    devices.run_due(AUTH, mon)
    devices.run_due(AUTH, mon.replace(second=40))  # same minute again: skipped
    sent = [c for c in wyze if "set_property" in c[1]]
    assert len(sent) == 1 and sent[0][2]["pvalue"] == "1" and sent[0][2]["device_mac"] == "PLUG-0001"


def test_scheduler_never_touches_a_lock_even_from_a_hand_edited_file(store):
    import json
    json.dump({"YD.LO1.x": {"type": "Lock", "model": "YD.LO1", "entries": [{"time": "07:30", "on": False, "days": [0, 1, 2, 3, 4, 5, 6]}]}}, open(devices.SCHEDULE_FILE, "w"))
    devices.run_due(AUTH, datetime(2026, 10, 26, 7, 30))
    assert not devices._ran


def test_next_run_wraps_to_the_following_week():
    now = datetime(2026, 10, 26, 8, 0)  # Monday after the 07:30 run
    nxt = devices._next_run({"time": "07:30", "days": [1]}, now)
    assert datetime.fromtimestamp(nxt) == datetime(2026, 11, 2, 7, 30)


def lock(bat):
    return {"mac": "L", "type": "Lock", "model": "YD.LO1", "state": {"battery": bat}}


def test_battery_estimate_needs_three_days_then_extrapolates(store):
    now = 10 * DAY
    for i in range(0, 2 * DAY + 1, 3600):  # two days: not enough
        devices.record_history([lock(100 - i / DAY)], now + i)
    assert devices.battery_days_left("L", now + 2 * DAY) is None
    for i in range(2 * DAY, 4 * DAY + 1, 3600):  # one point a day lost
        devices.record_history([lock(100 - i / DAY)], now + i)
    assert devices.battery_days_left("L", now + 4 * DAY) == 96
    assert "temp" not in devices._history["L"][0]


def test_battery_estimate_is_blank_when_not_draining(store):
    for i in range(0, 5 * DAY, 3600):
        devices.record_history([lock(80)], 10 * DAY + i)
    assert devices.battery_days_left("L", 15 * DAY) is None
    assert devices.battery_days_left("nope") is None


def test_battery_days_appear_in_the_device_list(wyze, store):
    now = devices.time.time()
    devices._history["YD.LO1.abc123"] = [{"t": int(now - (4 - i) * DAY), "bat": 90 - 5 * i} for i in range(5)]
    lock_dev = next(d for d in devices.list_devices(AUTH, fresh=True) if d["type"] == "Lock")
    assert lock_dev["battery_days"] == pytest.approx(16, abs=2)

