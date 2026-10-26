# Sorts after test_web_devices.py; reuses its fakes.
import time

import pytest
from test_web_devices import AUTH, OBJECTS, Resp, client  # noqa: F401
from wyzebridge import devices

NOW = 1_800_000_000
RECENT = int(time.time()) - 600  # the route reads the real clock


@pytest.fixture
def earth(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setitem(devices._cache, "at", 0.0)
    monkeypatch.setattr(devices, "HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr(devices, "_history", {"CO_EA1_1": [{"t": RECENT, "temp": 70.0, "hum": 41.0, "run": "heat"}]})

    def get(url, params=None, **kw):
        calls.append((url.rsplit("/", 1)[1], params))
        if url.endswith("/work_statistics"):
            return Resp({"code": 1, "data": [{"time": 0, "heat": 90 * 60000, "cool": 0}, {"time": 1, "heat": 30 * 60000, "cool": 5 * 60000 + 59999}]})
        return Resp({"code": 1, "data": {"props": {"temperature": "70"}}})

    def post(url, json=None, data=None, **kw):
        return Resp({"code": 1, "data": OBJECTS if url.endswith("/get_object_list") else {}})

    monkeypatch.setattr(devices, "get", get)
    monkeypatch.setattr(devices, "post", post)
    monkeypatch.setattr("wyzecam.api.post", post)
    return calls


def test_run_time_is_asked_in_milliseconds_and_summed(earth):
    assert devices.wyze_run_minutes(AUTH, "CO_EA1_1", 168, NOW) == {"heat_minutes": 120, "cool_minutes": 5}
    (name, p), = earth
    assert name == "work_statistics" and p["device_id"] == "CO_EA1_1" and "nonce" in p
    assert (p["begin_time"], p["last_time"]) == ((NOW - 7 * 86400) * 1000, NOW * 1000)


def test_page_uses_wyzes_run_time_and_the_bridges_readings(client, earth):
    data = client.get("/api/devices/history?mac=CO_EA1_1&hours=24").json
    assert data == {"points": [{"t": RECENT, "temp": 70.0, "hum": 41.0, "run": "heat"}], "heat_minutes": 120, "cool_minutes": 5}


def test_page_falls_back_and_room_sensors_never_ask(client, earth, monkeypatch):
    def down(*a, **kw):
        raise devices.RequestException("no route")

    monkeypatch.setattr(devices, "wyze_run_minutes", down)
    assert client.get("/api/devices/history?mac=CO_EA1_1&hours=24").json["heat_minutes"] == 10  # the bridge's own count
    earth.clear()
    client.get("/api/devices/history?mac=CO_TH1_1&hours=24")
    assert not [c for c in earth if c[0] == "work_statistics"]
