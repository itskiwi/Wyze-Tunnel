# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
import os
from base64 import b64encode
from datetime import datetime
from unittest import mock

import pytest
from werkzeug.security import generate_password_hash

NOW = 1761375600 + 3 * 3600  # a few hours past a local midnight


@pytest.fixture
def client(monkeypatch, tmp_path):
    import frontend
    from wyzebridge import guests, sd_copy, web_ui

    monkeypatch.setattr(guests, "GUESTS_FILE", str(tmp_path / "guests.json"))
    monkeypatch.setattr(sd_copy, "SETTINGS_FILE", str(tmp_path / "sd_copy.json"))
    monkeypatch.setattr(sd_copy, "ROOT", tmp_path / "sd_copy")
    monkeypatch.setattr(sd_copy, "_done", {})
    monkeypatch.setattr(sd_copy, "status", {"state": "off", "message": "", "last": 0, "copied": 0})
    for cls in {web_ui.WbAuth, frontend.WbAuth}:
        monkeypatch.setattr(cls, "enabled", True)
        monkeypatch.setattr(cls, "username", "owner")
        monkeypatch.setattr(cls, "_hashed_pass", generate_password_hash("ownerpass"))
    with mock.patch.object(frontend, "WyzeBridge") as bridge:
        app = frontend.create_app()
    c = app.test_client()
    c.environ_base["HTTP_AUTHORIZATION"] = "Basic " + b64encode(b"owner:ownerpass").decode()
    return frontend, bridge.return_value, c


def fake_stream(connected=True):
    from wyzebridge.wyze_stream import WyzeStream

    return mock.Mock(spec=WyzeStream, source="tutk", connected=connected, user=None, camera=None, options=None)


def test_window_wraps_midnight():
    from wyzebridge.sd_copy import in_window

    day = {"from": "01:00", "to": "05:00"}
    assert in_window(day, datetime(2026, 1, 1, 1, 0)) and not in_window(day, datetime(2026, 1, 1, 5, 0))
    night = {"from": "22:00", "to": "02:00"}
    assert in_window(night, datetime(2026, 1, 1, 23, 0)) and in_window(night, datetime(2026, 1, 2, 1, 0)) and not in_window(night, datetime(2026, 1, 1, 12, 0))


def test_settings_validate(client):
    _, wb, c = client
    wb.streams.streams = {"cam1": 1}
    assert c.post("/sd-copy/settings", json={"cams": ["cam1"], "from": "25:00", "to": "05:00", "max_gb": 20}).status_code == 400
    assert c.post("/sd-copy/settings", json={"cams": ["cam1"], "from": "01:00", "to": "05:00", "max_gb": 0}).status_code == 400
    resp = c.post("/sd-copy/settings", json={"cams": ["cam1", "nope"], "from": "02:00", "to": "04:00", "max_gb": 5})
    assert resp.json["settings"] == {"cams": ["cam1"], "from": "02:00", "to": "04:00", "max_gb": 5}
    assert c.get("/sd-copy/status").json["state"] == "off"


def test_copy_skips_done_stops_at_window_end_and_prunes(client, monkeypatch):
    from wyzebridge import sd_copy, sd_playback

    start = NOW - 86400
    seg = [[start, start + 1800]]  # three 10-minute clips
    streams = mock.Mock()
    streams.get.return_value = fake_stream()
    streams.send_cmd.side_effect = lambda cam, cmd, ts: {"value": {"sd_card": True, "segments": seg if ts == start else []}}
    (sd_copy.ROOT / "cam1").mkdir(parents=True)
    (sd_copy.ROOT / "cam1" / f"{start}.mp4").write_bytes(b"x")  # already copied
    saved = []

    def save(cam, user, camera, options, ts, until, out):
        saved.append((ts, until))
        open(out, "wb").write(b"y")
        return True

    monkeypatch.setattr(sd_playback, "save", save)
    settings = {"cams": ["cam1"], "from": "00:00", "to": "23:59", "max_gb": 20}
    assert sd_copy.copy_cam(streams, "cam1", settings, NOW) is True
    assert saved == [(start + 600, start + 1200), (start + 1200, start + 1800)]
    # Someone watching that camera's SD playback: nothing runs.
    saved.clear()
    (sd_copy.ROOT / "cam1" / f"{start + 600}.mp4").unlink()
    monkeypatch.setitem(sd_playback.players, "cam1-sd", (mock.Mock(is_alive=lambda: True), None))
    assert sd_copy.copy_cam(streams, "cam1", settings, NOW) is False and not saved
    sd_playback.players.clear()
    # Outside the window it stops before the first clip.
    assert sd_copy.copy_cam(streams, "cam1", {**settings, "from": "00:00", "to": "00:00"}, NOW) is False and not saved
    # A disconnected camera is skipped.
    streams.get.return_value = fake_stream(connected=False)
    assert sd_copy.copy_cam(streams, "cam1", settings, NOW) is False
    # The cap removes the oldest first.
    for i, name in enumerate(("1.mp4", "2.mp4")):
        f = sd_copy.ROOT / "cam1" / name
        f.write_bytes(b"z" * 1024**3 * 0 + b"z" * 10)
        os.utime(f, (i, i))
    sd_copy.prune(0)
    assert not sd_copy.copied_files()


def test_sd_play_refused_while_copying(client):
    frontend, wb, c = client
    from wyzebridge import sd_copy

    wb.streams.get.return_value = fake_stream()
    sd_copy.status.update(state="copying", message="cam1: Jan 01 02:00")
    assert c.post("/sd/cam1/play", json={"ts": NOW}).status_code == 409


def test_pages_render(client):
    _, wb, c = client
    wb.streams.streams = {}
    wb.api.total_cams = 0
    assert c.get("/sd-sync").status_code == 200
    assert c.get("/recordings").status_code == 200


def test_sync_play_and_stop(client):
    frontend, wb, c = client
    wb.streams.get.return_value = fake_stream()
    with mock.patch.object(frontend.sd_playback, "start") as start, mock.patch.object(frontend.sd_playback, "stop") as stop:
        assert c.post("/sd-sync/play", json={"cams": ["a"], "ts": NOW}).status_code == 400
        assert c.post("/sd-sync/play", json={"cams": list("abcde"), "ts": NOW}).status_code == 400
        resp = c.post("/sd-sync/play", json={"cams": ["a", "b"], "ts": NOW})
        assert resp.status_code == 200 and resp.json["cams"]["b"]["path"] == "b-sd"
        assert c.post("/sd-sync/play", json={"cams": ["a", "b"], "ts": "x"}).status_code == 400
        assert [call.args[0] for call in start.call_args_list] == ["a", "b"] and all(call.args[-1] == NOW for call in start.call_args_list)
        c.post("/sd-sync/stop", json={"cams": ["a", "b"]})
    assert [call.args[0] for call in stop.call_args_list] == ["a-sd", "b-sd"]


def test_owner_only(client):
    _, wb, c = client
    from wyzebridge import guests

    password = guests.add_guest("Kid", 0, "owner")
    family = {"Authorization": "Basic " + b64encode(f"Kid:{password}".encode()).decode()}
    for method, url in (("get", "/sd-sync"), ("post", "/sd-sync/play"), ("post", "/sd-sync/stop"), ("post", "/sd-copy/settings"), ("get", "/sd-copy/status")):
        assert getattr(c, method)(url, headers=family).status_code == 403, url
    c.environ_base.pop("HTTP_AUTHORIZATION")
    assert c.get("/sd-copy/status").status_code == 401


def test_new_features_have_one_home_each(client):
    """Clips, events, search and SD card share the Recordings switch; automations live on one page."""
    _, wb, c = client
    wb.streams.streams = {}
    wb.api.total_cams = 0
    for url in ("/recordings", "/history", "/search", "/sd-sync"):
        page = c.get(url).data.decode()
        assert 'aria-label="Recordings"' in page and 'href="sd-sync"' in page and 'href="history"' in page, url
        assert 'href="recordings" class="tab" aria-current=page' in page, url  # the Recordings tab stays lit
    rules = c.get("/rules").data.decode()
    assert "Made for your home" in rules and "Pause the heat when a door is open" in rules
    devices = c.get("/devices").data.decode()
    assert "Pause the heat" not in devices and 'href="rules"' in devices
    assert 'id="tamperOn"' in c.get("/alerts").data.decode()
