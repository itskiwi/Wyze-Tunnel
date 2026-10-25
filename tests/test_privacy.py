from datetime import time
from types import SimpleNamespace
from unittest import mock

import pytest
from wyzebridge import privacy


@pytest.fixture(autouse=True)
def privacy_file(tmp_path, monkeypatch):
    monkeypatch.setattr(privacy, "PRIVACY_FILE", str(tmp_path / "privacy.json"))


def stream(mac, sub=False):
    return SimpleNamespace(uri=mac.lower(), camera=SimpleNamespace(mac=mac), options=SimpleNamespace(substream=sub),
                           send_cmd=mock.Mock())


def test_save_validates():
    with pytest.raises(ValueError):
        privacy.save_privacy({"cams": ["ZZ"], "start": "22:00", "end": "07:00"}, {"AA"})
    with pytest.raises(ValueError):
        privacy.save_privacy({"cams": ["AA"], "start": "late"}, {"AA"})
    assert privacy.save_privacy({"cams": []}, {"AA"}) == {"cams": []}


def test_turns_off_at_start_and_on_at_end_only():
    privacy.save_privacy({"cams": ["AA"], "start": "22:00", "end": "07:00"}, {"AA", "BB"})
    a, b, sub = stream("AA"), stream("BB"), stream("AA", sub=True)
    streams = {"a": a, "b": b, "a-sub": sub}
    schedule = privacy.PrivacySchedule()

    schedule.check(streams, time(12, 0))  # startup outside the window: leave cameras alone
    a.send_cmd.assert_not_called()
    schedule.check(streams, time(22, 1))
    a.send_cmd.assert_called_once_with("power", "off")
    schedule.check(streams, time(23, 0))  # no repeat inside the window
    assert a.send_cmd.call_count == 1
    schedule.check(streams, time(7, 0))
    a.send_cmd.assert_called_with("power", "on")
    b.send_cmd.assert_not_called()
    sub.send_cmd.assert_not_called()


def test_startup_inside_window_turns_off():
    privacy.save_privacy({"cams": ["AA"], "start": "22:00", "end": "07:00"}, {"AA"})
    a = stream("AA")
    privacy.PrivacySchedule().check({"a": a}, time(2, 0))
    a.send_cmd.assert_called_once_with("power", "off")


def test_battery_saver_stops_streaming_instead_of_power(tmp_path, monkeypatch):
    path = str(tmp_path / "battery.json")
    privacy.save_privacy({"cams": ["AA"], "start": "22:00", "end": "07:00"}, {"AA"}, path)
    sent = []
    stream = SimpleNamespace(camera=SimpleNamespace(mac="AA"), options=SimpleNamespace(substream=False), uri="cam",
                             send_cmd=lambda *a: sent.append(a))
    saver = privacy.PrivacySchedule(path, ("state", "disable", "enable"))
    saver.check({"cam": stream}, time(23, 0))
    saver.check({"cam": stream}, time(8, 0))
    assert sent == [("state", "disable"), ("state", "enable")]
