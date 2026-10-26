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
    # Opening the link (or a link preview fetching it) only asks; the button press pairs.
    assert b"Pair this phone" in client.get(path, headers=IPHONE).data
    assert client.get(path, headers=IPHONE).status_code == 200
    resp = client.post(path, headers=IPHONE)
    assert resp.status_code == 302 and "pocket=1" in resp.location
    assert "HttpOnly" in resp.headers["Set-Cookie"]
    page = client.get("/?pocket=1")
    assert page.status_code == 200 and b"This phone is paired" in page.data
    assert [g["name"] for g in guests.list_guests()] == ["iPhone"]

    # One use only, and a second phone gets its own login.
    assert client.get(path, headers=IPHONE).status_code == 410
    assert client.post(path, headers=IPHONE).status_code == 410
    client2 = client.application.test_client()
    code2 = client.post("/pair", headers=owner).json["home"]["url"].rpartition("/")[2]
    client2.post("/pair/" + code2, headers=IPHONE)
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
        assert client.post("/pair/" + code).status_code == 410
    assert client.get("/pair/nope").status_code == 410


FAKE_AWAY = """#!/usr/bin/env python3
import json, sys
args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
status = {"state": "Running", "auth_url": "", "host": "wyze-tunnel.tail1234.ts.net", "public": "", "funnel_url": ""}
if "-funnel" in sys.argv:
    status["public"] = "https://wyze-tunnel.tail1234.ts.net"
json.dump(status, open(args["-status"], "w"))
sys.stdin.read()  # like wyze-away, it exits when the bridge closes its stdin
"""


def test_away_access_turns_on_and_heals(setup, tmp_path):  # noqa: F811
    client, _, _ = setup
    owner = basic("owner", "ownerpass")
    fake = tmp_path / "wyze-away"
    fake.write_text(FAKE_AWAY)
    fake.chmod(0o755)
    from wyzebridge import pocket

    with mock.patch.object(pocket, "AWAY_BIN", str(fake)), mock.patch.object(pocket, "AWAY_DIR", tmp_path / "ts"):
        assert not client.get("/away", headers=owner).json["on"]
        assert client.post("/away", json={"on": True}, headers=owner).json["on"]
        for _ in range(50):
            if pocket.Away.status()["state"] == "Running":
                break
            pocket.time.sleep(0.1)
        codes = client.post("/pair", headers=owner).json
        assert codes["anywhere"]["url"].startswith("https://wyze-tunnel.tail1234.ts.net/pair/")

        pocket.Away.proc.kill()
        pocket.Away.proc.wait()
        pocket.Away.health_check()
        assert pocket.Away.running()

        proc = pocket.Away.proc
        assert not client.post("/away", json={"on": False}, headers=owner).json["on"]
        assert proc.wait(5) is not None and not pocket.Away.running()
        pocket.Away.health_check()
        assert pocket.Away.proc is None


def test_public_link_only_lets_paired_phones_in(setup):  # noqa: F811
    client, _, _ = setup
    owner = basic("owner", "ownerpass")
    public = {"X-Wyze-Away-Public": "1"}
    # A typed password, even the right one, never works over the public link, and no other page shows.
    assert client.get("/", headers=owner | public).status_code == 403
    assert b"for paired phones" in client.get("/login", headers=public).data
    assert client.get("/api", headers=public | {"api": "nope"}).status_code == 403
    # Nothing that hints at the app: no shared files, no server name, and no indexing.
    assert client.get("/static/site.css", headers=public).status_code == 403
    assert b"Wyze" not in client.get("/", headers=public).data
    assert client.get("/robots.txt", headers=public).data == b"User-agent: *\nDisallow: /\n"
    assert "noindex" in client.get("/robots.txt", headers=public).headers["X-Robots-Tag"]

    code = client.post("/pair", headers=owner).json["home"]["url"].rpartition("/")[2]
    phone = client.application.test_client()
    # Scanned with the camera, the link arrives from another app: the browser marks it cross-site.
    assert phone.get("/pair/" + code, headers=IPHONE | public | {"Sec-Fetch-Site": "cross-site"}).status_code == 200
    assert phone.post("/pair/" + code, headers=IPHONE | public | {"Sec-Fetch-Site": "same-origin"}).status_code == 302
    assert phone.get("/", headers=public).status_code == 200
    assert phone.post("/away", json={"on": False}, headers=public).status_code == 403  # a family login, not the owner


def test_alerts_once_when_tailscale_signs_out(tmp_path):
    from wyzebridge import pocket

    states = iter(["Running", "NeedsLogin", "NeedsLogin"])
    with mock.patch.object(pocket, "AWAY_DIR", tmp_path), mock.patch.object(pocket.Away, "status", lambda: {"state": next(states)}), \
            mock.patch.object(pocket.webhooks, "send_webhook") as send:
        for _ in range(3):
            pocket.Away.check_sign_in()
    assert send.call_count == 1 and send.call_args.args[0] == "away"
