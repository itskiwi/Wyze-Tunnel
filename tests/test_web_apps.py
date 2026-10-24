# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
import json
import os
from base64 import b64encode
from unittest import mock

import pytest
from werkzeug.security import generate_password_hash

CAMS = {
    "front": {
        "nickname": "Front Door", "status": 3, "enabled": True, "motion": True, "motion_ts": 1700000000,
        "is_doorbell": True, "model_name": "Doorbell v2",
        "rtsp_url": "rtsp://h:8554/front", "hls_url": "http://h:8888/front/", "webrtc_url": None,
    }
}


@pytest.fixture
def client(monkeypatch, tmp_path):
    import frontend
    from wyzebridge import guests, integrations, mtx_server, web_ui

    monkeypatch.setattr(guests, "GUESTS_FILE", str(tmp_path / "guests.json"))
    monkeypatch.setattr(mtx_server, "MTX_CONFIG", str(tmp_path / "mediamtx.yml"))
    (tmp_path / "mediamtx.yml").write_text("authInternalUsers: []\n")
    monkeypatch.setattr(integrations, "APPS_FILE", str(tmp_path / "apps.json"))
    for cls in {web_ui.WbAuth, frontend.WbAuth}:
        monkeypatch.setattr(cls, "enabled", True)
        monkeypatch.setattr(cls, "username", "owner")
        monkeypatch.setattr(cls, "_hashed_pass", generate_password_hash("ownerpass"))
    with mock.patch.object(frontend, "WyzeBridge") as wb:
        app = frontend.create_app()
    streams = wb.return_value.streams
    streams.get.side_effect = lambda uri: mock.Mock() if uri in CAMS else None
    streams.send_cmd.return_value = {"status": "success"}
    c = app.test_client()
    c.environ_base["HTTP_AUTHORIZATION"] = "Basic " + b64encode(b"owner:ownerpass").decode()
    with mock.patch.object(web_ui, "all_cams", return_value={"cameras": CAMS}):
        yield c, streams, integrations, tmp_path


def make(c, scope, name="Test app"):
    resp = c.post("/apps/tokens", json={"name": name, "scope": scope})
    assert resp.status_code == 200, resp.json
    return {"Authorization": f"Bearer {resp.json['token']}"}


def as_app(c, headers, method, url, **kw):
    """A request from another app: no browser login, only the token."""
    return c.open(url, method=method, headers=headers, environ_base={"HTTP_AUTHORIZATION": headers["Authorization"]}, **kw)


def test_tokens_are_hashed_private_and_shown_once(client):
    c, _, integrations, tmp_path = client
    headers = make(c, "view")
    token = headers["Authorization"].split()[1]
    saved = (tmp_path / "apps.json").read_text()
    assert token not in saved
    assert oct(os.stat(tmp_path / "apps.json").st_mode)[-3:] == "600"
    assert "hash" not in json.dumps(integrations.list_tokens())
    page = c.get("/apps").get_data(as_text=True)
    assert "Test app" in page and token not in page
    assert c.post("/apps/tokens", json={"name": "x", "scope": "admin"}).status_code == 400
    assert c.post("/apps/tokens", json={"name": "", "scope": "view"}).status_code == 400


def test_view_token_reads_but_cannot_control(client):
    c, streams, integrations, _ = client
    h = make(c, "view")
    cams = as_app(c, h, "GET", "/api/v1/cameras").json
    assert cams[0]["id"] == "front" and cams[0]["streaming"] and cams[0]["features"]["doorbell"]
    key = integrations.stream_key()
    assert cams[0]["streams"]["rtsp"] == f"rtsp://apps:{key}@h:8554/front"
    assert as_app(c, h, "GET", "/api/v1/").json["scope"] == "view"
    assert as_app(c, h, "POST", "/api/v1/cameras/front/commands/spotlight", json={"value": "on"}).status_code == 403
    streams.send_cmd.assert_not_called()
    # Nothing outside /api/v1: not the old API, settings or the Apps page.
    for url in ("/api", "/apps", "/account", "/restart/all", "/api/v1x"):
        assert as_app(c, h, "GET", url).status_code in (401, 403, 404), url
    assert as_app(c, {"Authorization": "Bearer wt_nope"}, "GET", "/api/v1/cameras").status_code == 401


def test_control_token_sends_commands_and_removal_ends_access(client):
    c, streams, integrations, tmp_path = client
    h = make(c, "control")
    resp = as_app(c, h, "POST", "/api/v1/cameras/front/commands/spotlight", json={"value": "on"})
    assert resp.status_code == 200
    streams.send_cmd.assert_called_once_with("front", "spotlight", "on")
    assert as_app(c, h, "POST", "/api/v1/cameras/nope/commands/spotlight").status_code == 404

    mtx = (tmp_path / "mediamtx.yml").read_text()
    key = integrations.stream_key()
    assert "user: apps" in mtx and key in mtx
    token_id = integrations.list_tokens()[0]["id"]
    assert c.delete(f"/apps/tokens/{token_id}").status_code == 200
    assert as_app(c, h, "GET", "/api/v1/cameras").status_code == 401
    assert "user: apps" not in (tmp_path / "mediamtx.yml").read_text()  # stream login gone with the last token
    assert integrations.stream_key() != key


def test_family_and_cross_site_cannot_manage_apps(client):
    c, *_ = client
    from wyzebridge import guests

    password = guests.add_guest("Kid", 0, "owner")
    family = {"Authorization": "Basic " + b64encode(f"Kid:{password}".encode()).decode()}
    assert c.get("/apps", headers=family).status_code == 403
    assert c.post("/apps/tokens", json={"name": "x", "scope": "control"}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def rpc(c, h, method, params=None, id=1):
    return as_app(c, h, "POST", "/api/v1/mcp", json={"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}})


def test_mcp(client):
    c, streams, _, _ = client
    view = make(c, "view")
    init = rpc(c, view, "initialize", {"protocolVersion": "2025-03-26"}).json["result"]
    assert init["protocolVersion"] == "2025-03-26" and "tools" in init["capabilities"]
    note = as_app(c, view, "POST", "/api/v1/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note.status_code == 202
    names = [t["name"] for t in rpc(c, view, "tools/list").json["result"]["tools"]]
    assert "list_cameras" in names and "send_command" not in names
    listed = rpc(c, view, "tools/call", {"name": "list_cameras"}).json["result"]
    assert json.loads(listed["content"][0]["text"])[0]["name"] == "Front Door"
    denied = rpc(c, view, "tools/call", {"name": "send_command", "arguments": {"camera": "front", "command": "alarm"}})
    assert denied.json["result"]["isError"]
    streams.send_cmd.assert_not_called()

    control = make(c, "control", "Claude")
    ok = rpc(c, control, "tools/call", {"name": "send_command", "arguments": {"camera": "front", "command": "alarm", "value": "on"}})
    assert not ok.json["result"]["isError"]
    streams.send_cmd.assert_called_once_with("front", "alarm", "on")
    assert rpc(c, view, "nope").json["error"]["code"] == -32601
    assert as_app(c, view, "GET", "/api/v1/mcp").status_code == 405


def test_webhooks_signed_and_validated(client):
    c, _, integrations, _ = client
    assert c.post("/apps/hooks", json={"url": "ftp://x", "events": ["motion"]}).status_code == 400
    assert c.post("/apps/hooks", json={"url": "https://example.com/h", "events": []}).status_code == 400
    secret = c.post("/apps/hooks", json={"url": "https://example.com/h", "events": ["motion"]}).json["secret"]
    hook = integrations.list_hooks()[0]
    assert "secret" not in hook

    with mock.patch.object(integrations.requests, "post") as post, \
            mock.patch.object(integrations, "Thread", side_effect=lambda target, args, daemon: mock.Mock(start=lambda: target(*args))):
        post.return_value = mock.Mock(ok=True, status_code=200, reason="OK")
        from wyzebridge import webhooks

        webhooks.send_webhook("motion", "front", "Motion on Front Door")
        webhooks.send_webhook("offline", "front", "Front is offline")  # not subscribed
        webhooks.send_webhook("test", "Wyze Tunnel", "Phone alert test")  # the Alerts page test stays there
    post.assert_called_once()
    kw = post.call_args.kwargs
    assert kw["allow_redirects"] is False
    headers, body = kw["headers"], kw["data"]
    assert json.loads(body)["event"] == "motion"
    assert headers["X-Wyze-Tunnel-Signature"] == integrations.sign(secret, headers["X-Wyze-Tunnel-Timestamp"], body)

    with mock.patch.object(integrations.requests, "post", side_effect=integrations.requests.ConnectionError("x")):
        result = c.post(f"/apps/hooks/{hook['id']}/test").json
    assert result["ok"] is False and integrations.list_hooks()[0]["last"]["ok"] is False
    assert c.delete(f"/apps/hooks/{hook['id']}").status_code == 200 and integrations.list_hooks() == []


def test_new_token_for_same_app_replaces_old(client):
    c, _, integrations, _ = client
    old = make(c, "control", "Home Assistant")
    new = make(c, "control", "Home Assistant")
    assert [t["name"] for t in integrations.list_tokens()] == ["Home Assistant"]
    assert as_app(c, old, "GET", "/api/v1/").status_code == 401
    assert as_app(c, new, "GET", "/api/v1/").status_code == 200
