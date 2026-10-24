# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
from datetime import datetime, timedelta, timezone
from base64 import b64encode
from unittest import mock

import pytest
from werkzeug.security import generate_password_hash


@pytest.fixture
def client(monkeypatch, tmp_path):
    import frontend
    from wyzebridge import guests, mtx_server, privacy, shares, street_watch, web_ui

    monkeypatch.setattr(guests, "GUESTS_FILE", str(tmp_path / "guests.json"))
    monkeypatch.setattr(mtx_server, "MTX_CONFIG", str(tmp_path / "mediamtx.yml"))

    for cls in {web_ui.WbAuth, frontend.WbAuth}:
        monkeypatch.setattr(cls, "enabled", True)
        monkeypatch.setattr(cls, "username", "owner")
        monkeypatch.setattr(cls, "_hashed_pass", generate_password_hash("ownerpass"))
    monkeypatch.setattr(shares, "SHARES_FILE", str(tmp_path / "shares.json"))
    monkeypatch.setattr(street_watch, "STREET_FILE", str(tmp_path / "street.json"))
    monkeypatch.setattr(privacy, "PRIVACY_FILE", str(tmp_path / "privacy.json"))
    monkeypatch.setattr(street_watch, "_submits", {})
    with mock.patch.object(frontend, "WyzeBridge") as wb:
        app = frontend.create_app()
    wb.return_value.streams.get.return_value.camera.mac = "MAC1"
    client = app.test_client()
    client.environ_base["HTTP_AUTHORIZATION"] = "Basic " + b64encode(b"owner:ownerpass").decode()
    with mock.patch.object(frontend.webhooks, "send_webhook"):
        yield frontend, client


def ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def ask(client, url, **over):
    form = {"what": "Package taken", "contact": "555-0100", "start": ago(hours=2), "end": ago(hours=1, minutes=30), **over}
    return client.post("/" + url, json=form, headers={"Sec-Fetch-Site": "same-origin"})


def test_off_by_default_and_owner_only(client):
    _, c = client
    auth = c.environ_base.pop("HTTP_AUTHORIZATION")
    assert c.get("/street-watch").status_code == 401
    c.environ_base["HTTP_AUTHORIZATION"] = auth
    from wyzebridge import guests

    password = guests.add_guest("Kid", 0, "owner")  # family can do most things, but not this
    family = {"Authorization": "Basic " + b64encode(f"Kid:{password}".encode()).decode()}
    assert c.get("/street-watch", headers=family).status_code == 403
    assert c.post("/street-watch/links", json={"name": "Pat"}).status_code == 400  # off
    assert c.get("/street/anything").status_code == 404


def test_request_approve_share_without_sound(client):
    frontend, c = client
    c.post("/street-watch", json={"enabled": True})
    url = c.post("/street-watch/links", json={"name": "Pat"}).json["url"]

    page = c.get("/" + url, headers={"Sec-Fetch-Site": "cross-site"})  # opened from a chat app
    assert page.status_code == 200 and page.headers["X-Robots-Tag"].startswith("noindex")
    assert b"Pat" in page.data and b"cam1" not in page.data

    assert ask(c, url, start=ago(days=8), end=ago(days=8, minutes=-30)).status_code == 400  # too old
    assert ask(c, url, start=ago(hours=3), end=ago(hours=1)).status_code == 400  # over an hour
    assert ask(c, url, what="").status_code == 400
    assert ask(c, url).json == {"status": "sent"}
    assert c.post("/" + url, json={}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403  # CSRF
    for _ in range(2):
        ask(c, url)
    assert "3 requests waiting" in ask(c, url).json["error"]

    from wyzebridge import street_watch

    req = street_watch.state()["requests"][0]
    clip = {"cam": "cam1", "start": "2026-10-24T08:42:40Z", "duration": 60}
    share = c.post(f"/street-watch/requests/{req['id']}/approve", json=clip).json["url"]
    assert c.post(f"/street-watch/requests/{req['id']}/approve", json=clip).status_code == 400  # answered

    with mock.patch.object(frontend.subprocess, "Popen") as popen:
        popen.return_value.stdout.read.side_effect = [b"mp4", b""]
        assert c.get("/" + share).data == b"mp4"
    cmd = popen.call_args.args[0]
    assert "-an" in cmd and "path=cam1" in cmd[cmd.index("-i") + 1]
    assert street_watch.state()["requests"][0]["views"] == 1

    c.post(f"/street-watch/requests/{req['id']}/revoke")
    assert c.get("/" + share).status_code == 404


def test_privacy_schedule_blocks_clip(client):
    _, c = client
    from wyzebridge import privacy, street_watch

    privacy.save_privacy({"cams": ["MAC1"], "start": "00:00", "end": "23:59"}, {"MAC1"})
    street_watch.set_enabled(True)
    token = street_watch.add_link("Pat")
    ask(c, f"street/{token}")
    req = street_watch.state()["requests"][0]
    resp = c.post(f"/street-watch/requests/{req['id']}/approve", json={"cam": "cam1", "start": ago(hours=2), "duration": 60})
    assert "privacy schedule" in resp.json["error"]


def test_turning_off_ends_everything(client):
    _, c = client
    from wyzebridge import shares, street_watch

    street_watch.set_enabled(True)
    token = street_watch.add_link("Pat")
    ask(c, f"street/{token}")
    req = street_watch.state()["requests"][0]
    share = street_watch.approve(req["id"], "cam1", "MAC2", "2026-10-24T08:42:40Z", 60)
    c.post("/street-watch", json={"enabled": False})
    assert shares.get_share(share) is None
    assert c.get(f"/street/{token}").status_code == 404
    assert street_watch.state()["requests"] == [] and street_watch.state()["log"]


def test_owner_page_renders_requests(client):
    frontend, c = client
    from wyzebridge import street_watch

    street_watch.set_enabled(True)
    ask(c, f"street/{street_watch.add_link('Pat')}")
    with mock.patch.object(frontend.web_ui, "all_cams", return_value={"cameras": {"cam1": {"record": True, "nickname": "Porch"}}}):
        page = c.get("/street-watch")
    assert page.status_code == 200 and b"Package taken" in page.data and b"Porch" in page.data
