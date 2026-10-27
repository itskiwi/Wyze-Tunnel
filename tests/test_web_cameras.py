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


@pytest.mark.parametrize("cookies, checked", [
    ({}, "snapshot"),
    ({"show_video": "1"}, "live"),
    ({"show_video": "1", "autoplay": "1"}, "autoplay"),
])
def test_live_page_preview_switch(setup, monkeypatch, cookies, checked):
    import re
    from wyzebridge import web_ui

    client, _, _ = setup
    cams = {"front": {"nickname": "Front", "enabled": True, "connected": True, "on_demand": False}}
    monkeypatch.setattr(web_ui, "all_cams", lambda *_: {"enabled": 1, "available": 1, "total": 1, "cameras": cams})
    for name, value in cookies.items():
        client.set_cookie(name, value)
    html = client.get("/live", headers={"Authorization": "Basic dTpw"}).text
    on = re.findall(r'data-action="(snapshot|live|autoplay)" role="radio" aria-checked="true"', html)
    assert on == [checked]


def test_camera_page(setup, monkeypatch):
    from wyzebridge import web_ui

    client, _, _ = setup
    cams = {u: {"nickname": u.title(), "enabled": u != "off", "connected": True, "webrtc": True, "hls_page_url": "x/"} for u in ("front", "off", "back", "side")}
    monkeypatch.setattr(web_ui, "all_cams", lambda *_: {"enabled": 3, "available": 4, "total": 4, "cameras": cams})
    auth = {"Authorization": "Basic dTpw"}
    html = client.get("/camera?cam=back", headers=auth).text
    assert 'data-cam="back"' in html and "<h1>Back</h1>" in html
    assert 'data-prev="front"' in html and 'data-next="side"' in html  # the camera that's off is skipped
    assert 'data-control="1"' in html and "cp-talk" in html
    assert client.get("/camera?cam=nope", headers=auth).headers["Location"].endswith("live")
