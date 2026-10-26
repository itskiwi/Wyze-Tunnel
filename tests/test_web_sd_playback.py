# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
from base64 import b64encode
from struct import pack
from unittest import mock

import pytest
from werkzeug.security import generate_password_hash
from wyzecam.tutk import tutk_protocol

DAY = 1761375600  # a local midnight


def record_info(start, minutes, sd_card=1):
    bits = sum(1 << m for m in minutes).to_bytes(90, "little")
    return bytes([1, 1, sd_card]) + pack("<IHH", start, 720, 90) + bits


def test_record_info_encode_and_parse():
    msg = tutk_protocol.K10350GetRecordInfo(DAY)
    assert msg.encode()[16:] == pack("<BBIH", 1, 1, DAY, 720)
    assert msg.expected_response_code == 10351
    resp = msg.parse_response(record_info(DAY, [0, 1, 2, 9, *range(700, 720)]))
    assert resp == {"sd_card": True, "start": DAY, "segments": [[DAY, DAY + 180], [DAY + 540, DAY + 600], [DAY + 42000, DAY + 43200]]}
    assert msg.parse_response(record_info(DAY, [], sd_card=0))["sd_card"] is False
    with pytest.raises(tutk_protocol.TutkWyzeProtocolError):
        msg.parse_response(b"\x01\x02")


def test_replay_encode():
    assert tutk_protocol.K10150Replay(DAY).encode()[16:] == pack("<BBI", 1, 1, DAY)
    assert tutk_protocol.K10150Replay(DAY, start=False).encode()[16:] == pack("<BBI", 2, 1, DAY)
    assert tutk_protocol.K10150Replay(DAY).parse_response(b"\x01") is True
    assert tutk_protocol.K10150Replay(DAY).parse_response(b"\x02") is False


def test_pace_holds_recorded_speed():
    from wyzebridge.sd_playback import pace

    delay, anchor = pace(None, 1000.0, 50.0)
    assert (delay, anchor) == (0.0, (50.0, 1000.0))
    assert pace(anchor, 1001.0, 50.25) == (0.75, anchor)  # card reads ahead: wait
    assert pace(anchor, 1001.0, 51.5) == (0.0, anchor)  # a little behind: send now
    assert pace(anchor, 1600.0, 51.0) == (0.0, (51.0, 1600.0))  # gap between recordings: jump


@pytest.fixture
def client(monkeypatch, tmp_path):
    import frontend
    from wyzebridge import guests, web_ui

    monkeypatch.setattr(guests, "GUESTS_FILE", str(tmp_path / "guests.json"))
    for cls in {web_ui.WbAuth, frontend.WbAuth}:
        monkeypatch.setattr(cls, "enabled", True)
        monkeypatch.setattr(cls, "username", "owner")
        monkeypatch.setattr(cls, "_hashed_pass", generate_password_hash("ownerpass"))
    with mock.patch.object(frontend, "WyzeBridge") as bridge:
        app = frontend.create_app()
    c = app.test_client()
    c.environ_base["HTTP_AUTHORIZATION"] = "Basic " + b64encode(b"owner:ownerpass").decode()
    return frontend, bridge.return_value, c


def test_day_merges_halves(client):
    _, wb, c = client
    halves = {DAY: [[DAY + 600, DAY + 43200]], DAY + 43200: [[DAY + 43200, DAY + 43500], [DAY + 50000, DAY + 50060]]}
    wb.streams.send_cmd.side_effect = lambda cam, cmd, start: {"value": {"sd_card": True, "start": start, "segments": halves[start]}}
    assert c.get("/sd/cam1?start=x").status_code == 400
    resp = c.get(f"/sd/cam1?start={DAY}")
    assert resp.json == {"sd_card": True, "segments": [[DAY + 600, DAY + 43500], [DAY + 50000, DAY + 50060]]}
    assert wb.streams.send_cmd.call_args_list[0].args == ("cam1", "sd_minutes", DAY)
    wb.streams.send_cmd.side_effect = lambda *_: {"response": "timed out"}
    assert c.get(f"/sd/cam1?start={DAY}").json == {"error": "timed out"}


def test_play_and_stop(client):
    frontend, wb, c = client
    from wyzebridge.wyze_stream import WyzeStream

    wb.streams.get.return_value = mock.Mock(spec=WyzeStream, source="tutk", user=None)
    with mock.patch.object(frontend.sd_playback, "start") as start, mock.patch.object(frontend.sd_playback, "stop") as stop:
        assert c.post("/sd/cam1/play", json={"ts": "soon"}).status_code == 400
        assert c.post("/sd/cam1/play", json={"ts": 9999999999}).status_code == 400  # the future
        assert c.post("/sd/cam1/play", json={"ts": DAY}).json == {"path": "cam1-sd", "hls": "hls/cam1-sd/index.m3u8"}
        assert c.post("/sd/cam1/stop").status_code == 200
    wb.mtx.add_path.assert_called_once_with("cam1-sd", False)
    assert start.call_args.args[0] == "cam1" and start.call_args.args[-1] == DAY
    stop.assert_called_once_with("cam1-sd")
    wb.streams.get.return_value = mock.Mock(spec=WyzeStream, source="go2rtc")
    assert c.post("/sd/cam1/play", json={"ts": DAY}).status_code == 400


def test_owner_only(client):
    _, _, c = client
    from wyzebridge import guests

    password = guests.add_guest("Kid", 0, "owner")
    family = {"Authorization": "Basic " + b64encode(f"Kid:{password}".encode()).decode()}
    assert c.get(f"/sd/cam1?start={DAY}", headers=family).status_code == 403
    assert c.post("/sd/cam1/play", json={"ts": DAY}, headers=family).status_code == 403
    c.environ_base.pop("HTTP_AUTHORIZATION")
    assert c.get(f"/sd/cam1?start={DAY}").status_code == 401
    assert c.get(f"/sd/cam1?start={DAY}&api=key").status_code == 401  # the API key is for /api, not this
