import io
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

os.environ["TOKEN_PATH"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).parents[1] / "app"))

import pytest  # noqa: E402
import requests  # noqa: E402


def make_client():
    import frontend

    with mock.patch.object(frontend, "WyzeBridge"):
        app = frontend.create_app()
    return frontend, app.test_client()


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    from wyzebridge import web_ui

    # test_security reloads wyzebridge.auth, leaving web_ui with the old WbAuth. This file must sort after it
    # so frontend is first imported with the reloaded one, which test_wyze_login patches.
    monkeypatch.setattr(web_ui.WbAuth, "api", "key")


def fake_resp(status, body=b"", json=None):
    resp = requests.Response()
    resp.status_code, resp.raw = status, io.BytesIO(body)
    if json is not None:
        resp.json = lambda: json
    return resp


def test_list_strips_internal_url():
    frontend, client = make_client()
    seg = {"start": "2026-10-24T08:42:40Z", "duration": 60.0, "url": "http://127.0.0.1:9996/get"}
    with mock.patch.object(frontend.requests, "get", return_value=fake_resp(200, json=[seg])) as get:
        resp = client.get("/recordings/cam1?api=key")
    assert resp.json == [{"start": seg["start"], "duration": 60.0}]
    assert get.call_args.kwargs["params"] == {"path": "cam1"}


def test_list_empty_when_no_recordings():
    frontend, client = make_client()
    with mock.patch.object(frontend.requests, "get", return_value=fake_resp(404, b"no recordings")):
        assert client.get("/recordings/cam1?api=key").json == []


def test_play_requires_range_and_streams_mp4():
    frontend, client = make_client()
    assert client.get("/recordings/cam1/play?api=key").status_code == 400
    with mock.patch.object(frontend.requests, "get", return_value=fake_resp(200, b"mp4data")) as get:
        resp = client.get("/recordings/cam1/play?api=key&start=2026-10-24T08:42:40Z&duration=5")
    assert resp.mimetype == "video/mp4" and resp.data == b"mp4data"
    assert get.call_args.kwargs["params"]["format"] == "mp4"


def test_requires_auth():
    _, client = make_client()
    assert client.get("/recordings/cam1").status_code == 401


def test_share_link_plays_without_login_until_it_ends(tmp_path, monkeypatch):
    from wyzebridge import shares

    monkeypatch.setattr(shares, "SHARES_FILE", str(tmp_path / "shares.json"))
    frontend, client = make_client()
    start = "2026-10-24T08:42:40.123456Z"
    bad = client.post("/recordings/cam1/share?api=key", json={"start": start, "duration": 9999})
    assert bad.status_code == 400
    url = client.post("/recordings/cam1/share?api=key", json={"start": start, "duration": 60}).json["url"]
    with mock.patch.object(frontend.requests, "get", return_value=fake_resp(200, b"mp4data")) as get:
        resp = client.get("/" + url, headers={"Sec-Fetch-Site": "cross-site"})  # opened from a chat app
    assert resp.data == b"mp4data" and get.call_args.kwargs["params"]["start"] == start
    assert client.get("/share/nope").status_code == 404
    with mock.patch("wyzebridge.shares.time.time", return_value=2**40):
        assert client.get("/" + url).status_code == 404


def test_live_hls_goes_through_the_bridge_login(monkeypatch):
    from wyzebridge import web_ui

    frontend, client = make_client()
    assert client.get("/hls/cam1/stream.m3u8").status_code == 401
    monkeypatch.setattr(web_ui.WbAuth, "enabled", False)
    with mock.patch.object(frontend.requests, "get", return_value=fake_resp(200, b"#EXTM3U")) as get:
        resp = client.get("/hls/cam1/seg0.mp4?_HLS_msn=3")
    assert resp.data == b"#EXTM3U"
    assert get.call_args.args[0] == "http://127.0.0.1:8888/cam1/seg0.mp4"
    assert get.call_args.kwargs["params"]["_HLS_msn"] == "3"


def test_recording_switches_save_and_restart_cameras(tmp_path, monkeypatch):
    from wyzebridge import recording, web_ui

    monkeypatch.setattr(recording, "SETTINGS_FILE", str(tmp_path / "record.json"))
    monkeypatch.setattr(web_ui.WbAuth, "enabled", False)
    import frontend

    with mock.patch.object(frontend, "WyzeBridge") as bridge:
        client = frontend.create_app().test_client()
    bridge.return_value.streams.streams = {"front": None, "back": None}
    bridge.return_value.api.total_cams = 2
    cams = {"front": {"nickname": "Front", "record": False}, "back": {"nickname": "Back", "record": False}}
    with mock.patch.object(web_ui, "all_cams", return_value={"cameras": cams}):
        page = client.get("/recordings").data.decode()
    assert "RECORD_ALL" not in page and 'value="front"' in page and "Choose cameras to record" in page

    assert client.post("/recordings/settings", json={"keep_days": 99}).status_code == 400
    with mock.patch.object(frontend, "Thread") as thread:
        assert client.post("/recordings/settings", json={"cams": ["front", "gone"], "keep_days": 3}).json == {"status": "saved"}
    assert thread.call_args.kwargs["target"] == bridge.return_value.restart
    assert recording.wanted("front") and not recording.wanted("back") and recording.keep() == "259200s"
    recording.save_settings({"all": True}, ["front", "back"])
    assert recording.wanted("back") and recording.keep() == "604800s"  # 7 days by default


def test_disk_use_counts_clips(tmp_path, monkeypatch):
    from wyzebridge import recording

    monkeypatch.setattr(recording, "RECORD_PATH", str(tmp_path / "record" / "%path" / "%Y-%m-%d_%H-%M-%S-%f"))
    assert recording.record_root() == tmp_path / "record"
    assert recording.disk_use()["used"] == 0  # nothing recorded yet, folder missing
    (tmp_path / "record" / "front").mkdir(parents=True)
    (tmp_path / "record" / "front" / "a.mp4").write_bytes(b"x" * 10)
    use = recording.disk_use()
    assert use["used"] == 10 and use["free"] > 0
