# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
from base64 import b64encode
from unittest import mock

import pytest
import yaml
from werkzeug.security import generate_password_hash


@pytest.fixture
def client(monkeypatch, tmp_path):
    import frontend
    from wyzebridge import guests, mtx_server, phone_cams, recording, web_ui

    monkeypatch.setattr(guests, "GUESTS_FILE", str(tmp_path / "guests.json"))
    monkeypatch.setattr(mtx_server, "MTX_CONFIG", str(tmp_path / "mediamtx.yml"))
    monkeypatch.setattr(phone_cams, "PHONES_FILE", str(tmp_path / "phone_cams.json"))
    monkeypatch.setattr(recording, "SETTINGS_FILE", str(tmp_path / "record.json"))
    monkeypatch.setattr(frontend, "path_bytes", lambda: {"phone-nursery": {"in": 9, "out": 0, "ready": True}})
    for cls in {web_ui.WbAuth, frontend.WbAuth}:
        monkeypatch.setattr(cls, "enabled", True)
        monkeypatch.setattr(cls, "username", "owner")
        monkeypatch.setattr(cls, "_hashed_pass", generate_password_hash("ownerpass"))
    with mock.patch.object(frontend, "WyzeBridge") as bridge:
        app = frontend.create_app()
    wb = bridge.return_value
    wb.streams.streams = {"front-door": None}
    wb.streams.get.return_value = None
    client = app.test_client()
    client.environ_base["HTTP_AUTHORIZATION"] = "Basic " + b64encode(b"owner:ownerpass").decode()
    yield frontend, wb, client


def test_names(monkeypatch, tmp_path):
    from wyzebridge import phone_cams

    monkeypatch.setattr(phone_cams, "PHONES_FILE", str(tmp_path / "phone_cams.json"))
    cam = phone_cams.add(" Baby Room ", {"front-door"})
    assert cam["uri"] == "phone-baby-room" and cam["name"] == "Baby Room" and len(cam["token"]) >= 32
    for bad in ("", "x" * 33, "!!!", "Baby Room"):
        with pytest.raises(ValueError):
            phone_cams.add(bad, set())
    with pytest.raises(ValueError):
        phone_cams.add("Door", {"phone-door"})  # a stream by that name already exists
    assert phone_cams.by_token(cam["token"]) == cam
    assert phone_cams.by_token("wrong") is None and phone_cams.by_token("näh") is None
    phone_cams.remove(cam["uri"])
    assert phone_cams.uris() == []


def test_owner_only(client):
    _, _, c = client
    from wyzebridge import guests

    password = guests.add_guest("Kid", 0, "owner")  # family can do most things, but not add cameras
    family = {"Authorization": "Basic " + b64encode(f"Kid:{password}".encode()).decode()}
    assert c.get("/phone-cams", headers=family).status_code == 403
    assert c.post("/phone-cams", json={"name": "Nursery"}, headers=family).status_code == 403
    assert c.delete("/phone-cams/phone-nursery", headers=family).status_code == 403
    c.environ_base.pop("HTTP_AUTHORIZATION")
    assert c.get("/phone-cams").status_code == 401


def test_add_open_and_publish(client):
    frontend, wb, c = client
    assert c.post("/phone-cams", json={"name": "Nursery"}).json == {"uri": "phone-nursery"}
    wb.add_phone_path.assert_called_once_with("phone-nursery")
    assert c.post("/phone-cams", json={"name": "Front Door"}).status_code == 200  # phone-front-door, not front-door

    from wyzebridge import phone_cams

    token = phone_cams.list_cams()[0]["token"]
    page = c.get("/phone-cams").get_data(as_text=True)
    assert f"/phone-cam/{token}" in page and "<svg" in page and "Streaming now" in page
    assert "phones only share their camera" in page  # plain http
    assert "phones only share" not in c.get("/phone-cams", base_url="https://tunnel.example.com").get_data(as_text=True)

    # The phone has no login: the link is enough, and works when opened from a chat app.
    c.environ_base.pop("HTTP_AUTHORIZATION")
    phone = c.get(f"/phone-cam/{token}", headers={"Sec-Fetch-Site": "cross-site"})
    assert phone.status_code == 200 and b"Nursery" in phone.data and phone.headers["X-Robots-Tag"].startswith("noindex")
    assert c.get("/phone-cam/nope").status_code == 404

    answer = mock.Mock(status_code=201, content=b"v=0 answer")
    with mock.patch.object(frontend.requests, "post", return_value=answer) as post:
        resp = c.post(f"/phone-cam/{token}/whip", data=b"v=0 offer", headers={"Sec-Fetch-Site": "same-origin"})
    assert resp.status_code == 201 and resp.data == b"v=0 answer" and resp.mimetype == "application/sdp"
    assert post.call_args.args[0] == "http://127.0.0.1:8889/phone-nursery/whip" and post.call_args.kwargs["data"] == b"v=0 offer"
    with mock.patch.object(frontend.requests, "post") as post:
        assert c.post("/phone-cam/nope/whip", data=b"v=0").status_code == 404
        assert c.post(f"/phone-cam/{token}/whip", data=b"v=0", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    post.assert_not_called()


def test_record_and_remove(client):
    _, wb, c = client
    c.post("/phone-cams", json={"name": "Nursery"})
    assert c.delete("/phone-cams/front-door").status_code == 404  # a Wyze camera's path stays
    assert c.post("/recordings/settings", json={"cams": ["phone-nursery"]}).status_code == 200
    from wyzebridge import recording

    assert recording.wanted("phone-nursery")
    page = c.get("/recordings").get_data(as_text=True)
    assert '<option value="phone-nursery"' in page
    assert c.delete("/phone-cams/phone-nursery").json == {"status": "removed"}
    wb.mtx.remove_path.assert_called_once_with("phone-nursery")
    assert c.get("/recordings/phone-nursery/thumb?start=2026-01-01T00:00:00Z").status_code == 404


def test_mediamtx_path(monkeypatch, tmp_path):
    from wyzebridge import mtx_event, mtx_server

    monkeypatch.setattr(mtx_server, "MTX_CONFIG", str(tmp_path / "mediamtx.yml"))
    (tmp_path / "mediamtx.yml").write_text("paths: {}\n")
    mtx = mtx_server.MtxServer()
    mtx.add_path("front-door")
    mtx.add_path("phone-nursery", False)
    mtx.record("phone-nursery", "604800s")
    paths = yaml.safe_load((tmp_path / "mediamtx.yml").read_text())["paths"]
    assert paths["phone-nursery"]["record"] and "runOnDemand" not in paths["phone-nursery"]
    mtx.remove_path("phone-nursery")
    assert list(yaml.safe_load((tmp_path / "mediamtx.yml").read_text())["paths"]) == ["front-door"]

    event = mtx_event.RtspEvent.__new__(mtx_event.RtspEvent)
    event.streams = {}
    event.log_event("phone-nursery,notready")  # a phone stopping isn't a Wyze stream to stop
