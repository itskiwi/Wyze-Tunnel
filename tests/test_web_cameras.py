# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
from unittest import mock

import pytest
from wyzecam.api_models import WyzeCamera


def cam(mac, name):
    return WyzeCamera(p2p_id=None, p2p_type=None, ip=None, enr=None, mac=mac, product_model="WYZE_CAKP2JFUS",
                      nickname=name, timezone_name=None, firmware_ver=None, dtls=None, parent_dtls=None,
                      parent_enr=None, parent_mac=None, thumbnail=None)


@pytest.fixture
def setup(monkeypatch, tmp_path):
    import frontend
    from wyzebridge import web_ui, wyze_api

    monkeypatch.setattr(wyze_api, "HIDDEN_CAMS_FILE", str(tmp_path / "hidden.json"))
    monkeypatch.setattr(wyze_api, "CAM_NAMES_FILE", str(tmp_path / "names.json"))
    monkeypatch.setattr(web_ui.auth, "verify_password_callback", lambda user, password: True)
    with mock.patch.object(frontend, "WyzeBridge") as WB, mock.patch.object(frontend, "Thread") as thread:
        WB.return_value.api.get_cameras.return_value = [cam("AA", "Front"), cam("BB", "Back")]
        yield frontend.create_app().test_client(), wyze_api, thread


def test_hide_and_show_cameras(setup):
    client, wyze_api, thread = setup
    assert [c["hidden"] for c in client.get("/cameras/visibility").json] == [False, False]

    assert client.post("/cameras/visibility", json={"hidden": ["BB"]}).json == {"status": "saved"}
    assert wyze_api.load_hidden_cams() == {"BB"}
    assert thread.return_value.start.called  # streams restart without the hidden camera
    assert [c.nickname for c in wyze_api.filter_cams([cam("AA", "Front"), cam("BB", "Back")])] == ["Front"]

    assert client.post("/cameras/visibility", json={"hidden": ["AA", "BB"]}).status_code == 400
    assert client.post("/cameras/visibility", json={"hidden": ["ZZ"]}).status_code == 400
    assert wyze_api.load_hidden_cams() == {"BB"}


def test_rename_camera(setup):
    client, wyze_api, _ = setup
    front = cam("AA", "Front")
    assert client.post("/cameras/name", json={"mac": "AA", "name": " Porch "}).json == {"status": "saved"}
    assert wyze_api.display_name(front) == "Porch"
    assert front.name_uri == "front"  # stream address doesn't change
    assert client.get("/cameras/visibility").json[0]["nickname"] == "Porch"

    assert client.post("/cameras/name", json={"mac": "ZZ", "name": "x"}).status_code == 400
    assert client.post("/cameras/name", json={"mac": "AA", "name": "x" * 65}).status_code == 400
    client.post("/cameras/name", json={"mac": "AA", "name": ""})
    assert wyze_api.display_name(front) == "Front"
