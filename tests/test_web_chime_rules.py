# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
from base64 import b64encode
from types import SimpleNamespace
from unittest import mock

import pytest
import yaml
from werkzeug.security import generate_password_hash


def basic(user, password):
    return {"Authorization": "Basic " + b64encode(f"{user}:{password}".encode()).decode()}


OWNER = basic("owner", "ownerpass")
API_KEY = {"api": "apikey"}
RULES = {"status": "success", "rules": [
    {"id": "auto_1", "name": "Night", "enabled": True, "shortcut": False},
    {"id": "hot_1", "name": "Goodnight", "enabled": True, "shortcut": True},
]}


@pytest.fixture
def web(monkeypatch, tmp_path):
    import frontend
    from wyzebridge import guests, mtx_server, web_ui

    monkeypatch.setattr(guests, "GUESTS_FILE", str(tmp_path / "guests.json"))
    mtx = tmp_path / "mediamtx.yml"
    mtx.write_text(yaml.safe_dump({"authInternalUsers": [{"user": "wb", "pass": "api"}]}))
    monkeypatch.setattr(mtx_server, "MTX_CONFIG", str(mtx))
    for cls in {web_ui.WbAuth, frontend.WbAuth}:
        monkeypatch.setattr(cls, "enabled", True)
        monkeypatch.setattr(cls, "username", "owner")
        monkeypatch.setattr(cls, "_hashed_pass", generate_password_hash("ownerpass"))
        monkeypatch.setattr(cls, "api", "apikey")
    with mock.patch.object(frontend, "WyzeBridge") as bridge:
        wb = bridge.return_value
        cam = SimpleNamespace(is_doorbell=True, mac="DB1", name_uri="front")
        other = SimpleNamespace(is_doorbell=False, mac="C1", name_uri="yard")
        wb.streams.get.side_effect = lambda n: {"front": SimpleNamespace(camera=cam), "yard": SimpleNamespace(camera=other)}.get(n)
        wb.streams.send_cmd.return_value = {"status": "success", "value": "on"}
        wb.api.set_chime.return_value = {"status": "success"}
        wb.api.get_rules.return_value = RULES
        wb.api.run_shortcut.return_value = {"status": "success"}
        client = frontend.create_app().test_client()
        family = client.post("/guests", json={"name": "Mom", "hours": 0}, headers=OWNER).json["password"]
        guest = client.post("/guests", json={"name": "Sam", "hours": 24}, headers=OWNER).json["password"]
        yield client, wb, {"family": basic("Mom", family), "guest": basic("Sam", guest)}


def test_chime_change_is_owner_only(web):
    client, wb, who = web
    body = {"ring_sound": "off"}
    for headers in (who["family"], who["guest"]):
        assert client.post("/api/front/chime", json=body, headers=headers).status_code == 403
    assert client.post("/api/front/chime", json=body).status_code == 401
    assert not wb.streams.send_cmd.called
    # The API key is the owner's, like every other camera command.
    assert client.post("/api/front/chime", json=body, headers=API_KEY).status_code == 200


def test_chime_sends_the_apps_commands(web):
    client, wb, _ = web
    body = {"ring_sound": "on", "chime_type": "digital", "chime_seconds": 5, "chime_sound": 7, "chime_volume": 8}
    assert client.post("/api/front/chime", json=body, headers=OWNER).status_code == 200
    assert [c.args for c in wb.streams.send_cmd.call_args_list] == [
        ("front", "ring_sound", "on"), ("front", "doorbell_chime", "3,0,0"), ("front", "doorbell_chime", "0,0,5")]
    cam = wb.streams.get("front").camera
    wb.api.set_chime.assert_called_once_with(cam, 7, 8)
    wb.streams.send_cmd.return_value = {"response": "timed out"}
    assert client.post("/api/front/chime", json={"ring_sound": "on"}, headers=OWNER).status_code == 502


@pytest.mark.parametrize("body", [
    {}, [], {"nope": 1}, {"ring_sound": "loud"}, {"chime_type": "bell"}, {"chime_type": ["none"]}, {"chime_seconds": 0}, {"chime_seconds": 10},
    {"chime_seconds": "5"}, {"chime_seconds": True}, {"chime_sound": 0, "chime_volume": 4}, {"chime_sound": 20, "chime_volume": 4},
    {"chime_sound": 3}, {"chime_volume": 4}, {"chime_sound": 3, "chime_volume": 9}, {"chime_sound": 3, "chime_volume": -1},
])
def test_chime_input_is_checked(web, body):
    client, wb, _ = web
    assert client.post("/api/front/chime", json=body, headers=OWNER).status_code == 400
    assert not wb.streams.send_cmd.called and not wb.api.set_chime.called


def test_chime_only_for_doorbells_and_readable_when_signed_in(web):
    client, wb, who = web
    assert client.post("/api/yard/chime", json={"ring_sound": "on"}, headers=OWNER).status_code == 404
    assert client.get("/api/nope/chime", headers=OWNER).status_code == 404
    wb.streams.send_cmd.return_value = {"status": "success", "value": "on"}
    assert client.get("/api/front/chime", headers=OWNER).json == {"ring_sound": "on", "doorbell_chime": "on"}
    assert client.get("/api/front/chime", headers=who["guest"]).status_code == 403


def test_rules_list_for_signed_in_users_but_not_guests(web):
    client, wb, who = web
    assert client.get("/api/rules", headers=OWNER).json == RULES
    assert client.get("/api/rules", headers=who["family"]).json == RULES
    assert client.get("/api/rules", headers=who["guest"]).status_code == 403
    assert client.get("/api/rules").status_code == 401
    assert b"Goodnight" not in client.get("/rules", headers=OWNER).data  # names load through the API, as text


def test_running_a_shortcut_needs_the_owners_own_password(web):
    client, wb, who = web
    for headers in (who["family"], who["guest"], API_KEY, basic("owner", "wrong"), {}):
        assert client.post("/api/rules/hot_1/run", headers=headers).status_code in (401, 403)
    assert not wb.api.run_shortcut.called
    assert client.post("/api/rules/hot_1/run", headers=OWNER).status_code == 200
    wb.api.run_shortcut.assert_called_once_with("hot_1")


def test_only_listed_shortcuts_run(web):
    client, wb, _ = web
    assert client.post("/api/rules/auto_1/run", headers=OWNER).status_code == 404  # a rule, not a shortcut
    assert client.post("/api/rules/other/run", headers=OWNER).status_code == 404
    wb.api.run_shortcut.return_value = {"status": "error", "response": "boom"}
    assert client.post("/api/rules/hot_1/run", headers=OWNER).status_code == 502
    wb.api.run_shortcut.reset_mock()
    client.get("/api/rules/hot_1/run", headers=OWNER)  # only POST runs one
    assert not wb.api.run_shortcut.called


# Wyze cloud and camera pieces, with fakes.

class Resp:
    headers = {}
    request = SimpleNamespace(method="POST", path_url="/")

    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data

    def raise_for_status(self):
        pass


@pytest.fixture
def cloud(monkeypatch):
    from wyzecam import api

    calls = []
    monkeypatch.setattr(api, "API_STATS", api.Counter())

    def post(url, json=None, data=None, **kw):
        calls.append((url, json))
        if url.endswith("/get_object_list"):
            return Resp({"code": 1, "data": {"device_list": [
                {"product_model": "CHIME", "mac": "CH1", "parent_device_mac": "DB1"},
                {"product_model": "CHIME", "mac": "CH2", "parent_device_mac": "OTHER"}]}})
        if url.endswith("/get_auto_list"):
            return Resp({"code": 1, "data": {"auto_list": [
                {"auto_id": "hot_1", "auto_name": "Goodnight", "is_enable": 1, "is_hot_button": 1, "action_list": [{"device_mac": "SECRET"}]},
                {"auto_id": "auto_1", "auto_name": "Night", "is_enable": 0, "is_hot_button": 0}]}})
        return Resp({"code": 1, "data": {}})

    monkeypatch.setattr(api, "post", post)
    return calls


def fake_api():
    from wyzebridge.wyze_api import WyzeApi

    api = WyzeApi.__new__(WyzeApi)
    api.auth = SimpleNamespace(access_token="tok", phone_id="phone")
    return api


def test_chime_property_list_goes_to_the_paired_chime_only(cloud):
    cam = SimpleNamespace(mac="DB1", name_uri="front")
    assert fake_api().set_chime(cam, 7, 40)["status"] == "success"
    url, body = cloud[-1]
    assert url.endswith("/v2/device/set_property_list")
    assert (body["device_mac"], body["device_model"]) == ("CH1", "CHIME")
    assert body["property_list"] == [{"pid": "P1351", "pvalue": 40}, {"pid": "P1352", "pvalue": 7}, {"pid": "P1353", "pvalue": 1}]
    assert sum(u.endswith("/set_property_list") for u, _ in cloud) == 1
    assert fake_api().set_chime(SimpleNamespace(mac="NONE", name_uri="x"), 1, 1)["status"] == "error"


def test_rules_listing_hides_actions_and_running_posts_the_id(cloud):
    api = fake_api()
    assert api.get_rules()["rules"] == [
        {"id": "hot_1", "name": "Goodnight", "enabled": True, "shortcut": True},
        {"id": "auto_1", "name": "Night", "enabled": False, "shortcut": False}]
    assert api.run_shortcut("hot_1")["status"] == "success"
    url, body = cloud[-1]
    assert url.endswith("/v2/auto/run_auto") and body["auto_id"] == "hot_1"


def test_doorbell_tutk_messages_match_the_app():
    from wyzebridge.wyze_control import parse_cmd
    from wyzecam.tutk.tutk_protocol import K12082GetDb2DdqParameter, K12088GetDb2RingSound

    def sent(cmd):
        _, msg, _ = parse_cmd(cmd, "debug")
        return msg.code, msg.encode()[16:]

    assert sent(("doorbell_chime", "3,0,0")) == (12080, bytes([3, 0, 0]))
    assert sent(("doorbell_chime", "0,0,9")) == (12080, bytes([0, 0, 9]))
    assert sent(("ring_sound", "on")) == (12086, bytes([1]))
    assert sent(("ring_sound", "off")) == (12086, bytes([2]))
    assert sent("doorbell_chime")[0] == 12082 and sent("ring_sound")[0] == 12088
    assert K12082GetDb2DdqParameter().parse_response(bytes([2, 1, 4])) == {"type": "mechanical", "seconds": 4}
    assert K12088GetDb2RingSound().parse_response(bytes([2])) == "off"


def test_owner_password_is_not_the_api_key_or_a_guest(web):
    import frontend
    from wyzebridge import web_ui

    _, _, who = web
    app = frontend.Flask("t")

    def ok(headers):
        with app.test_request_context("/", headers=headers):
            return web_ui.owner_password()

    assert ok(OWNER) is True
    assert ok(basic("owner", "wrong")) is False and ok(who["family"]) is False and ok(API_KEY) is False and ok({}) is False
