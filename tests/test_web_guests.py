# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
from base64 import b64encode
from unittest import mock

import pytest
import yaml
from werkzeug.security import generate_password_hash


def basic(user, password):
    return {"Authorization": "Basic " + b64encode(f"{user}:{password}".encode()).decode()}


@pytest.fixture
def setup(monkeypatch, tmp_path):
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
    with mock.patch.object(frontend, "WyzeBridge"):
        yield frontend.create_app().test_client(), guests, mtx


def test_guest_can_watch_but_not_change(setup):
    client, guests, mtx = setup
    owner = basic("owner", "ownerpass")
    assert client.post("/guests", json={"name": "Sam", "hours": 5}, headers=owner).status_code == 400
    password = client.post("/guests", json={"name": "Sam", "hours": 24}, headers=owner).json["password"]
    users = yaml.safe_load(mtx.read_text())["authInternalUsers"]
    assert {"user": "guest", "pass": guests.stream_key(), "permissions": [{"action": "read"}]} in users

    sam = basic("Sam", password)
    page = client.get("/", headers=sam)
    assert page.status_code == 200 and b"Watching as Sam" in page.data and b"apikey" not in page.data
    signal = client.get("/signaling/cam", headers=sam).json
    assert signal["servers"][0]["credential"] == guests.stream_key()
    for path in ("/api/cam/power/off", "/account", "/settings/backup", "/cameras"):
        assert client.get(path, headers=sam).status_code == 403
    assert client.post("/guests", json={"name": "Eve", "hours": 1}, headers=sam).status_code == 403
    assert client.get("/", headers=basic("Sam", "wrong")).status_code == 401

    old_key = guests.stream_key()
    assert client.delete("/guests/Sam", headers=owner).json["status"] == "removed"
    assert client.get("/", headers=sam).status_code == 401
    assert guests.stream_key() != old_key
    assert all(u.get("user") != "guest" for u in yaml.safe_load(mtx.read_text())["authInternalUsers"])


def test_guest_login_ends_on_time(setup):
    client, guests, _ = setup
    password = guests.add_guest("Sam", 1, "owner")
    with mock.patch("wyzebridge.guests.time.time", return_value=guests._load()["guests"][0]["expires"] + 1):
        assert client.get("/", headers=basic("Sam", password)).status_code == 401
        guests.prune()
    assert guests.list_guests() == [] and guests._load()["guests"] == []


def test_family_login_has_full_access_but_not_owner_login(setup):
    client, guests, _ = setup
    password = client.post("/guests", json={"name": "Mom", "hours": 0}, headers=basic("owner", "ownerpass")).json["password"]
    mom = basic("Mom", password)
    page = client.get("/", headers=mom)
    assert b"Signed in as Mom" in page.data and b"apikey" not in page.data
    assert client.get("/cameras", headers=mom).status_code == 200
    assert client.post("/account", json={"username": "x", "password": "longenough"}, headers=mom).status_code == 403
    with mock.patch("wyzebridge.guests.time.time", return_value=2**40):
        assert client.get("/cameras", headers=mom).status_code == 200  # never ends
    client.delete("/guests/Mom", headers=basic("owner", "ownerpass"))
    assert client.get("/", headers=mom).status_code == 401
