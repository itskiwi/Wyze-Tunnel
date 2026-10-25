# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
import os
from base64 import b64encode
from unittest import mock

import pytest
import yaml
from werkzeug.security import generate_password_hash
from wyzebridge import go2rtc, homekit


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(homekit, "HOMEKIT_FILE", str(tmp_path / "homekit.json"))
    monkeypatch.setattr(go2rtc, "GO2RTC_CONFIG", str(tmp_path / "go2rtc.yaml"))
    return tmp_path


def test_setup_uri_matches_go2rtc():
    # go2rtc's links page: category << 31 | 2 << 27 | pin, base 36, padded to 9, then the setup id.
    uri = homekit.setup_uri("front", "13579246")
    payload = 17 << 31 | 2 << 27 | 13579246
    assert uri.startswith("X-HM://") and len(uri) == 7 + 9 + 4
    assert int(uri[7:16], 36) == payload
    assert homekit.pin_text("13579246") == "135-79-246"


def test_enable_makes_private_secure_accessory(store):
    homekit.set_enabled("front", True)
    cam = homekit.cameras()["front"]
    assert len(cam["pin"]) == 8 and cam["pin"] not in homekit.INSECURE_PINS
    assert oct(os.stat(store / "homekit.json").st_mode)[-3:] == "600"
    homekit.set_enabled("front", True)
    assert homekit.cameras()["front"] == cam  # turning on again keeps the code
    homekit.set_enabled("front", False)
    assert homekit.cameras() == {}


def test_config_opens_only_pairing_paths(store):
    server = go2rtc.Go2rtcServer()
    server.names = {"front": "Front Door", "back": "Back"}
    assert server.config()["api"] == {"listen": f"127.0.0.1:{go2rtc.API_PORT}"}
    assert "homekit" not in server.config()
    homekit.set_enabled("front", True)
    cfg = server.config()
    assert cfg["api"]["allow_paths"] == ["/pair-setup", "/pair-verify"]
    assert cfg["srtp"]["listen"] == f":{go2rtc.SRTP_PORT}"
    assert cfg["streams"] == {"front_homekit": "rtsp://127.0.0.1:8554/front"}
    acc = cfg["homekit"]["front_homekit"]
    assert acc["name"] == "Front Door" and acc["device_private"] == homekit.cameras()["front"]["private"]


def test_pairings_survive_rewrite_but_not_off_and_on(store):
    server = go2rtc.Go2rtcServer()
    server.names = {"front": "Front Door"}
    homekit.set_enabled("front", True)
    cfg = server.config()
    cfg["homekit"]["front_homekit"]["pairings"] = ["client_id=x&client_public=ab&permissions=1"]  # as go2rtc writes it
    (store / "go2rtc.yaml").write_text(yaml.safe_dump(cfg))
    homekit.keep_pairings(go2rtc.GO2RTC_CONFIG)
    assert homekit.cameras()["front"]["pairings"] == ["client_id=x&client_public=ab&permissions=1"]
    homekit.set_enabled("front", False)
    homekit.set_enabled("front", True)
    homekit.keep_pairings(go2rtc.GO2RTC_CONFIG)  # old file still has the old accessory's pairing
    assert homekit.cameras()["front"]["pairings"] == []


def test_apps_page_toggle(store, monkeypatch):
    import frontend
    from wyzebridge import web_ui
    from wyzebridge.wyze_stream import WyzeStream

    for cls in {web_ui.WbAuth, frontend.WbAuth}:
        monkeypatch.setattr(cls, "enabled", True)
        monkeypatch.setattr(cls, "username", "owner")
        monkeypatch.setattr(cls, "_hashed_pass", generate_password_hash("ownerpass"))
    monkeypatch.setattr(go2rtc, "go2rtc_available", lambda: True)
    monkeypatch.setattr(WyzeStream.go2rtc, "names", {"front": "Front Door"})
    with mock.patch.object(frontend, "WyzeBridge"):
        c = frontend.create_app().test_client()
    c.environ_base["HTTP_AUTHORIZATION"] = "Basic " + b64encode(b"owner:ownerpass").decode()
    with mock.patch.object(go2rtc.Go2rtcServer, "restart") as restart:
        assert c.post("/apps/homekit/front", json={"on": True}).status_code == 200
        assert c.post("/apps/homekit/nope", json={"on": True}).status_code == 404
    restart.assert_called_once()
    page = c.get("/apps").get_data(as_text=True)
    assert homekit.pin_text(homekit.cameras()["front"]["pin"]) in page and "Ready to add" in page
