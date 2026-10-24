# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
import json
from unittest import mock

import pytest
import requests
from wyzebridge import network, wyze_api
from wyzecam.api_models import WyzeCamera


def cam(mac, ip="192.168.1.20"):
    return WyzeCamera(p2p_id="UID" + mac, p2p_type=4, ip=ip, enr="secret", mac=mac, product_model="HL_CAM4",
                      nickname="Cam " + mac, timezone_name=None, firmware_ver="4.52", dtls=1, parent_dtls=None,
                      parent_enr=None, parent_mac=None, thumbnail="https://x/t.jpg")


def test_saved_lan_keys_cover_offline(monkeypatch, tmp_path):
    monkeypatch.setattr(wyze_api, "LAN_KEYS_FILE", str(tmp_path / "lan_keys.json"))
    wyze_api.save_lan_keys([cam("AA")])
    saved = json.loads((tmp_path / "lan_keys.json").read_text())
    assert saved[0]["enr"] == "secret" and "camera_info" not in saved[0]

    api = wyze_api.WyzeApi()
    monkeypatch.setattr(wyze_api.WyzeApi, "fetch_cameras", lambda self, fresh_data=False: None)  # Wyze unreachable
    cams = api.get_cameras()
    assert [c.mac for c in cams] == ["AA"] and cams[0].ip == "192.168.1.20"
    assert api.get_camera(cams[0].name_uri).enr == "secret"


def test_refresh_offline_keeps_tokens(monkeypatch):
    api = wyze_api.WyzeApi()
    api.auth = object()
    monkeypatch.setattr(wyze_api.WyzeApi, "auth_locked", property(lambda self: False))
    monkeypatch.setattr(wyze_api.wyzecam, "refresh_token", mock.Mock(side_effect=requests.ConnectionError("down")))
    monkeypatch.setattr(wyze_api.WyzeApi, "login", mock.Mock())
    assert api.refresh_token() is None
    assert api.auth is not None and not wyze_api.WyzeApi.login.called


def test_outbound_requests_are_recorded(monkeypatch):
    monkeypatch.setattr(network, "SEEN", {})
    monkeypatch.setattr(network, "_send", mock.Mock(side_effect=requests.ConnectionError()))
    with pytest.raises(requests.ConnectionError):
        requests.get("https://api.wyzecam.com/app/v2/home_page/get_object_list")
    entry = network.SEEN["api.wyzecam.com"]
    assert entry["count"] == 1 and entry["error"] == "ConnectionError" and not entry["local"]
    assert network.cloud_down()


def test_network_page(monkeypatch):
    import frontend
    from wyzebridge import web_ui

    monkeypatch.setattr(web_ui.auth, "verify_password_callback", lambda user, password: True)
    monkeypatch.setattr(web_ui, "all_cams", lambda *a: {"cameras": {"front": {"source": "lan", "ip": "192.168.1.20"}}})
    monkeypatch.setattr(network, "SEEN", {"ntfy.sh": {"host": "ntfy.sh", "purpose": "Phone alerts", "local": False, "count": 2, "error": "", "last": 0}})
    with mock.patch.object(frontend, "WyzeBridge"):
        page = frontend.create_app().test_client().get("/network").get_data(as_text=True)
    assert "ntfy.sh" in page and "Phone alerts" in page and "192.168.1.20" in page
