# Sorts after test_web_devices.py; reuses its fakes.
import json

import pytest
from test_web_devices import AUTH, Resp, client, wyze  # noqa: F401
from wyzebridge import devices

PRESETS = json.dumps({"home": {"heat": "68", "cool": "75"}, "away": {"heat": "62", "cool": "80"}, "sleep": {"heat": "64", "cool": "77"}})


def thermostat(**state):
    return {"mac": "T", "model": "CO_EA1", "type": "Thermostat", "name": "Hall", "state": {"config_scenario": PRESETS, "app_version": "1.2.0"} | state}


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(devices, "set_thermostat", lambda auth, did, props: out.append((did, props)))
    return out


def test_presets_carry_their_set_points_and_explicit_ones_win(sent):
    devices.control(AUTH, thermostat(), {"preset": "away"})
    assert sent[-1] == ("T", {"current_scenario": "away", "heat_sp": "62", "cool_sp": "80"})
    devices.control(AUTH, thermostat(), {"preset": "sleep", "heat_sp": 66})
    assert sent[-1][1] == {"current_scenario": "sleep", "heat_sp": "66", "cool_sp": "77"}


def test_fan_hold_emergency_heat_and_filters(sent, monkeypatch):
    monkeypatch.setattr(devices.time, "time", lambda: 1000)
    devices.control(AUTH, thermostat(), {"fan_mode": "on", "fancirc_time": 15, "emheat": True})
    assert sent[-1][1] == {"fan_mode": "on", "fancirc_time": "15", "emheat": "1"}
    devices.control(AUTH, thermostat(), {"hold": 2})
    assert sent[-1][1] == {"dev_hold": "1", "dev_holdtime": str(1000 + 7200)}
    devices.control(AUTH, thermostat(), {"hold": 0})
    assert sent[-1][1] == {"dev_hold": "1", "dev_holdtime": "0"}
    devices.control(AUTH, thermostat(), {"hold": False})
    assert sent[-1][1] == {"dev_hold": "0", "asw_hold": "0", "dev_holdtime": "0"}
    devices.control(AUTH, thermostat(), {"filter_reset": 2})
    assert sent[-1][1] == {"filter_2": "2"}
    devices.control(AUTH, thermostat(app_version="1.1.1"), {"filter_reset": 1})
    assert sent[-1][1] == {"filter_1": "1"}  # older firmware


@pytest.mark.parametrize("bad", [{"preset": "party"}, {"fan_mode": "high"}, {"fancirc_time": 7}, {"fancirc_time": 35}, {"emheat": "1"},
                                 {"hold": True}, {"hold": 25}, {"filter_reset": 6}, {}])
def test_bad_thermostat_input(sent, bad):
    with pytest.raises(ValueError):
        devices.control(AUTH, thermostat(), bad)
    assert not sent


def test_filters_use_the_apps_month_and_skip_unused_ones(monkeypatch):
    def earth_get(auth, path, params):
        if path == "device_info":
            return {"settings": {"filters": {"filter_1": {"filter_name": "Furnace", "filter_size": "16x25x1", "filter_life": "3", "filter_used": "0"}}}}
        return {"props": {"filter_1": "1", "filtercnt_1": str(3 * 43200 * 60 * 96 // 100), "filter_2": "0", "filtercnt_2": "0"}}

    monkeypatch.setattr(devices, "earth_get", earth_get)
    assert devices.filters(AUTH, "T") == [{"n": 1, "name": "Furnace", "size": "16x25x1", "life_months": 3.0, "used": 96, "replace": True}]


def test_room_sensors_show_and_change_whether_the_thermostat_uses_them(wyze, monkeypatch, sent):
    real = devices.earth_props

    def props(auth, did, keys, extra=None):
        out = real(auth, did, keys, extra)
        return out | {"sensor_using": json.dumps({"CO_TH1_1": "manual_down"})} if did == "CO_EA1_1" else out

    monkeypatch.setattr(devices, "earth_props", props)
    room = devices.find(AUTH, "CO_TH1_1")
    assert room["state"]["in_use"] is False
    assert devices.control(AUTH, room, {"in_use": True}) == "used for the thermostat"
    assert sent[-1] == ("CO_EA1_1", {"sensor_using": '{"CO_TH1_1":"manual_up"}'})


def test_filters_endpoint_is_for_the_thermostat_only(client, wyze, monkeypatch):
    monkeypatch.setattr(devices, "filters", lambda auth, mac: [{"n": 1}])
    assert client.get("/api/devices/filters?mac=CO_EA1_1").json == {"filters": [{"n": 1}]}
    assert client.get("/api/devices/filters?mac=PLUG-0001").status_code == 404
