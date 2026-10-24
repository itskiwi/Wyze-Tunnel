# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
import re
from unittest import mock

from test_web_guests import basic, setup  # noqa: F401

IPHONE = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)"}


def test_pair_phone_with_qr_code(setup):  # noqa: F811
    client, guests, _ = setup
    owner = basic("owner", "ownerpass")
    assert client.get("/").status_code == 401
    assert client.post("/pair").status_code == 401

    with mock.patch("wyzebridge.pocket.tailscale_host", return_value="pc.tail1234.ts.net"):
        codes = client.post("/pair", headers=owner, base_url="http://localhost:5000").json
    assert "<svg" in codes["home"]["qr"]
    assert codes["anywhere"]["url"].startswith("http://pc.tail1234.ts.net:5000/pair/")
    home = codes["home"]["url"]
    assert "localhost" not in home and re.fullmatch(r"http://[\d.]+:5000/pair/[\w-]+", home)

    path = "/pair/" + home.rpartition("/")[2]
    resp = client.get(path, headers=IPHONE)
    assert resp.status_code == 302 and "pocket=1" in resp.location
    assert "HttpOnly" in resp.headers["Set-Cookie"]
    page = client.get("/?pocket=1")
    assert page.status_code == 200 and b"This phone is paired" in page.data
    assert [g["name"] for g in guests.list_guests()] == ["iPhone"]

    # One use only, and a second phone gets its own login.
    assert client.get(path, headers=IPHONE).status_code == 410
    client2 = client.application.test_client()
    code2 = client.post("/pair", headers=owner).json["home"]["url"].rpartition("/")[2]
    client2.get("/pair/" + code2, headers=IPHONE)
    assert {g["name"] for g in guests.list_guests()} == {"iPhone", "iPhone 2"}

    # Removing it on the Login page unpairs it.
    client.delete("/guests/iPhone", headers=owner)
    assert client.get("/").status_code == 401
    assert client2.get("/").status_code == 200
    assert client2.post("/pair").status_code == 403


def test_pair_code_expires(setup):  # noqa: F811
    client, _, _ = setup
    code = client.post("/pair", headers=basic("owner", "ownerpass")).json["home"]["url"].rpartition("/")[2]
    with mock.patch("wyzebridge.pocket.time.time", return_value=10**12):
        assert client.get("/pair/" + code).status_code == 410
    assert client.get("/pair/nope").status_code == 410
