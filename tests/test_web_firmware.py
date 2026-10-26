# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
import json
from base64 import b64encode
from types import SimpleNamespace
from unittest import mock

import pytest
from werkzeug.security import generate_password_hash


def basic(user, password):
    return {"Authorization": "Basic " + b64encode(f"{user}:{password}".encode()).decode()}


OWNER = basic("owner", "ownerpass")
CAM = SimpleNamespace(mac="AABB", product_model="WYZEC1-JZ", firmware_ver="4.36.9.139", name_uri="front")


@pytest.fixture
def setup(monkeypatch, tmp_path):
    import frontend
    from wyzebridge import guests, web_ui

    monkeypatch.setattr(guests, "GUESTS_FILE", str(tmp_path / "guests.json"))
    for cls in {web_ui.WbAuth, frontend.WbAuth}:
        monkeypatch.setattr(cls, "enabled", True)
        monkeypatch.setattr(cls, "username", "owner")
        monkeypatch.setattr(cls, "_hashed_pass", generate_password_hash("ownerpass"))
        monkeypatch.setattr(cls, "api", "apikey")
    with mock.patch.object(frontend, "WyzeBridge") as bridge:
        api = bridge.return_value.api
        api.get_camera.return_value = CAM
        api.check_firmware.return_value = {"status": "success", "current": CAM.firmware_ver, "latest": "4.36.10.4054", "update": True, "id": 7}
        api.begin_firmware_upgrade.return_value = {"status": "success", "response": "started"}
        yield frontend.create_app().test_client(), api, guests


def install(client, headers, version="4.36.10.4054"):
    return client.post("/api/front/firmware/install", json={"version": version}, headers=headers)


def test_check_is_read_only_and_owner_only(setup):
    client, api, guests = setup
    assert client.get("/api/front/firmware", headers=OWNER).json["update"] is True
    api.begin_firmware_upgrade.assert_not_called()
    api.check_firmware.reset_mock()
    sam = basic("Sam", guests.add_guest("Sam", 1, "owner"))
    assert client.get("/api/front/firmware", headers=sam).status_code == 403
    api.check_firmware.assert_not_called()


def test_install_starts_the_offered_update_for_the_owner(setup):
    client, api, _ = setup
    assert install(client, OWNER).json["status"] == "success"
    api.begin_firmware_upgrade.assert_called_once_with(CAM, api.check_firmware.return_value)


def test_install_refused_for_everyone_but_the_owner_password(setup):
    client, api, guests = setup
    guest = basic("Sam", guests.add_guest("Sam", 1, "owner"))
    family = basic("Mom", guests.add_guest("Mom", 0, "owner"))
    for headers in (guest, family, {"api": "apikey"}, basic("owner", "wrong"), {}):
        assert install(client, headers).status_code in (401, 403)
    assert client.post("/api/front/firmware/install?api=apikey", json={"version": "4.36.10.4054"}).status_code in (401, 403)
    api.get_camera.assert_not_called()
    api.check_firmware.assert_not_called()
    api.begin_firmware_upgrade.assert_not_called()


def test_install_needs_a_current_offer_for_that_version(setup):
    client, api, _ = setup
    assert install(client, OWNER, "").status_code == 400
    assert install(client, OWNER, "9.9.9.9").status_code == 409  # not what Wyze offers
    api.check_firmware.return_value = {"status": "success", "latest": "4.36.9.139", "update": False}
    assert install(client, OWNER, "4.36.9.139").status_code == 409  # already current
    api.check_firmware.return_value = {"status": "error"}
    assert install(client, OWNER).status_code == 409
    api.begin_firmware_upgrade.assert_not_called()


def test_firmware_offer_compares_versions():
    from wyzebridge.wyze_api import firmware_offer

    assert firmware_offer("4.36.9.139", {"firmware_ver": "4.36.10.4054", "id": 7})["update"] is True
    assert firmware_offer("4.36.10.4054", {"firmware_ver": "4.36.10.4054", "id": 7})["update"] is False
    assert firmware_offer("4.36.10.4054", {"firmware_ver": "4.36.9.139"})["update"] is False
    assert firmware_offer("4.36.9.139", {})["update"] is False
    assert firmware_offer("4.36.9.139", {"firmware_ver": "beta"})["update"] is False


def test_wyze_calls_are_signed_and_use_the_app_fields(monkeypatch):
    from wyzebridge.wyze_api import WyzeApi
    from wyzecam import api

    sent = []
    resp = SimpleNamespace(headers={}, request=None, raise_for_status=lambda: None, json=lambda: {"code": "1", "data": {"firmware_ver": "4.36.10.4054", "id": 7}})
    monkeypatch.setattr(api, "API_STATS", api.Counter())  # keep the metrics test's counts clean
    monkeypatch.setattr(api, "post", lambda url, **kw: sent.append((url, kw)) or resp)
    wyze = WyzeApi.__new__(WyzeApi)
    wyze.auth = SimpleNamespace(access_token="tok", phone_id="ph")
    out = wyze.check_firmware(CAM)
    assert out["update"] is True and out["id"] == 7
    url, kw = sent[0]
    assert url.endswith("/app/v2/upgrade/get_upgrade_firmware") and kw["headers"]["signature2"]
    assert json.loads(kw["data"]) == {"device_id": "AABB", "device_model": "WYZEC1-JZ", "current_ver": "4.36.9.139", "testcode": "Official Version"}
    wyze.begin_firmware_upgrade(CAM, out)
    assert sent[1][0].endswith("/app/v2/upgrade/begin_upgrade")
    assert json.loads(sent[1][1]["data"])["targetVer"] == "4.36.10.4054"
