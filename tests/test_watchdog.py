from types import SimpleNamespace

from wyzebridge.stream import Watchdog


def cams(connected=True):
    return {"front": SimpleNamespace(connected=connected)}


def test_growing_bytes_never_restart():
    dog = Watchdog()
    assert not any(dog.check(cams(), {"front": {"in": n * 100}}, n * 15) for n in range(10))


def test_stalled_stream_restarts_once():
    dog = Watchdog()
    assert dog.check(cams(), {"front": {"in": 500}}, 0) == []
    assert dog.check(cams(), {"front": {"in": 500}}, 15) == []
    assert dog.check(cams(), {"front": {"in": 500}}, 31) == ["front"]
    assert dog.check(cams(), {"front": {"in": 500}}, 46) == []  # tracking starts over


def test_missing_api_data_never_restarts():
    dog = Watchdog()
    dog.check(cams(), {"front": {"in": 500}}, 0)
    assert dog.check(cams(), {}, 60) == []
    assert dog.check(cams(), {"other": {"in": 1}}, 120) == []


def test_disconnected_stream_is_ignored():
    dog = Watchdog()
    dog.check(cams(), {"front": {"in": 500}}, 0)
    assert dog.check(cams(False), {"front": {"in": 500}}, 60) == []
    assert dog.check(cams(), {"front": {"in": 500}}, 61) == []  # reconnected: fresh 30s


def test_restart_stalled_resets_go2rtc_stream(monkeypatch):
    from wyzebridge.stream import StreamManager

    resets, stops = [], []
    stream = SimpleNamespace(camera=SimpleNamespace(nickname="Front"), source="lan", stop=lambda: stops.append(1),
                             go2rtc=SimpleNamespace(reset=resets.append))
    mgr = StreamManager()
    mgr.streams["front"] = stream
    mgr.restart_stalled(["front"])
    assert stops == [1] and resets == ["front_lan"]
