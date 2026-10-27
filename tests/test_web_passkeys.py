# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
import hashlib
import json
import os

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from test_web_guests import basic, setup  # noqa: F401
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

HOST = "wyze-tunnel.tail1234.ts.net"
HTTPS = f"https://{HOST}"
LAN = "http://192.168.0.5:5000"
OWNER = basic("owner", "ownerpass")
PAGE = {"Accept": "text/html"}


class Authenticator:
    """A software passkey, like a phone's, that signs what the browser would send."""

    def __init__(self, rp_id=HOST, origin=HTTPS):
        self.key, self.id, self.rp_id, self.origin, self.count = ec.generate_private_key(ec.SECP256R1()), os.urandom(16), rp_id, origin, 0

    def _client(self, kind, options):
        return json.dumps({"type": kind, "challenge": options["challenge"], "origin": self.origin}).encode()

    def _auth_data(self, flags, extra=b""):
        self.count += 1
        return hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + self.count.to_bytes(4, "big") + extra

    def create(self, options):
        nums = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})
        auth = self._auth_data(0x45, bytes(16) + len(self.id).to_bytes(2, "big") + self.id + cose)  # user present + verified + key
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})
        cid = bytes_to_base64url(self.id)
        response = {"clientDataJSON": bytes_to_base64url(self._client("webauthn.create", options)), "attestationObject": bytes_to_base64url(att)}
        return {"id": cid, "rawId": cid, "type": "public-key", "response": response, "clientExtensionResults": {}}

    def get(self, options, key=None):
        client, auth = self._client("webauthn.get", options), self._auth_data(0x05)
        sig = (key or self.key).sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        cid = bytes_to_base64url(self.id)
        response = {k: bytes_to_base64url(v) for k, v in {"clientDataJSON": client, "authenticatorData": auth, "signature": sig}.items()}
        return {"id": cid, "rawId": cid, "type": "public-key", "response": response, "clientExtensionResults": {}}


@pytest.fixture
def app(setup, monkeypatch, tmp_path):  # noqa: F811
    from wyzebridge import passkeys

    monkeypatch.setattr(passkeys, "FILE", tmp_path / "passkeys.json")
    passkeys._challenges.clear()
    client, guests, _ = setup
    return client, guests, passkeys


def add_passkey(client, phone, headers=OWNER, **kw):
    options = client.post("/passkeys/register/options", headers=headers, **kw).json
    return client.post("/passkeys/register", json={"credential": phone.create(options), "name": "Phone"}, headers=headers, **kw)


def sign_in(client, phone, **kw):
    options = client.post("/passkeys/sign-in/options", base_url=HTTPS).json
    return client.post("/passkeys/sign-in/finish", json=phone.get(options, **kw), base_url=HTTPS)


def test_password_box_stays_until_a_passkey_is_added(app):
    client, _, _ = app
    # Desktop app and plain http: the browser's password box, as before.
    assert client.get("/", headers=PAGE, base_url=LAN).status_code == 401
    assert client.get("/", headers=PAGE, base_url=HTTPS).status_code == 401
    assert client.get("/", headers=OWNER).status_code == 200


def test_passkeys_need_https(app):
    client, _, passkeys = app
    resp = client.post("/passkeys/register/options", headers=OWNER, base_url=LAN)
    assert resp.status_code == 400 and "https" in resp.json["error"]
    assert client.get("/passkeys", headers=OWNER, base_url=LAN).json == {"keys": [], "secure": False}
    assert client.get("/passkeys", headers=OWNER).json["secure"] is True  # browsers allow passkeys on localhost
    # Through Wyze Tunnel's Tailscale https proxy, which talks plain http to the bridge.
    proxied = {"base_url": f"http://{HOST}", "headers": {**OWNER, "X-Forwarded-Proto": "https"}}
    assert client.get("/passkeys", **proxied).json["secure"] is True
    assert add_passkey(client, Authenticator(), **proxied).json["host"] == HOST


def test_add_then_sign_in_with_a_passkey(app):
    client, _, passkeys = app
    phone = Authenticator()
    added = add_passkey(client, phone, base_url=HTTPS)
    assert added.status_code == 200 and added.json["name"] == "Phone" and added.json["host"] == HOST
    assert "public_key" not in json.dumps(client.get("/passkeys", headers=OWNER, base_url=HTTPS).json)

    # On the https address, a page offers the passkey before the password box; http and the desktop app don't change.
    offer = client.get("/live?cam=x", headers=PAGE, base_url=HTTPS)
    assert offer.status_code == 302 and "/passkeys/sign-in?next=/live?cam%3Dx" in offer.location
    assert client.get("/", headers=PAGE, base_url=LAN).status_code == 401
    assert client.get("/api/sse_status", base_url=HTTPS).status_code == 401  # scripts and apps get the plain 401
    assert client.get("/passkeys/password?next=/live", headers=PAGE, base_url=HTTPS).status_code == 401
    assert client.get("/passkeys/password?next=//evil.example", headers=OWNER, base_url=HTTPS).location == "/"
    assert client.get("/passkeys/sign-in", base_url=HTTPS).status_code == 200

    done = sign_in(client, phone)
    assert done.status_code == 200
    cookie = done.headers["Set-Cookie"]
    assert all(flag in cookie for flag in ("Secure", "HttpOnly", "SameSite=Lax"))
    assert client.get("/", headers=PAGE, base_url=HTTPS).status_code == 200
    assert passkeys.list_keys()[0]["used"] > 0

    # Removing the passkey signs that browser out.
    key_id = passkeys.list_keys()[0]["id"]
    assert client.delete(f"/passkeys/{key_id}", headers=OWNER, base_url=HTTPS).json["status"] == "removed"
    assert client.get("/", headers=PAGE, base_url=HTTPS).status_code == 401


def test_forged_or_replayed_passkey_answers_fail(app):
    client, _, passkeys = app
    phone = Authenticator()
    add_passkey(client, phone, base_url=HTTPS)

    assert sign_in(client, phone, key=ec.generate_private_key(ec.SECP256R1())).status_code == 400  # wrong key
    assert sign_in(client, Authenticator()).status_code == 400  # a passkey this install never saw

    options = client.post("/passkeys/sign-in/options", base_url=HTTPS).json
    answer = phone.get(options)
    assert client.post("/passkeys/sign-in/finish", json=answer, base_url=HTTPS).status_code == 200
    client.delete_cookie(passkeys.COOKIE, domain=HOST)
    assert client.post("/passkeys/sign-in/finish", json=answer, base_url=HTTPS).status_code == 400  # replayed

    made_up = phone.get({"challenge": bytes_to_base64url(os.urandom(32))})
    assert client.post("/passkeys/sign-in/finish", json=made_up, base_url=HTTPS).status_code == 400
    phishing = Authenticator(origin="https://wyze-tunnel.evil.example")
    phishing.id, phishing.key = phone.id, phone.key
    assert sign_in(client, phishing).status_code == 400
    assert client.post("/passkeys/sign-in/finish", json=[1], base_url=HTTPS).status_code == 400


def test_sign_in_cookie_cant_be_forged(app):
    client, _, passkeys = app
    phone = Authenticator()
    add_passkey(client, phone, base_url=HTTPS)
    key_id = passkeys.list_keys()[0]["id"]
    for cookie in (f"{key_id}.9999999999.{'0' * 64}", f"{key_id}.1.x", "nope", ""):
        client.set_cookie(passkeys.COOKIE, cookie, domain=HOST)
        assert client.get("/", headers=PAGE, base_url=HTTPS).status_code == 302, cookie
    valid = sign_in(client, phone).headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
    _, expires, sig = valid.rsplit(".", 2)
    assert passkeys.signed_in(valid) and not passkeys.signed_in(f"{key_id}.{int(expires) + 1}.{sig}")


def test_only_the_owner_manages_passkeys(app):
    client, guests, passkeys = app
    family = basic("Kim", guests.add_guest("Kim", 0, "owner"))
    assert client.post("/passkeys/register/options", headers=family, base_url=HTTPS).status_code == 403
    assert client.get("/passkeys", headers=family, base_url=HTTPS).status_code == 403
    assert client.get("/passkeys", base_url=HTTPS).status_code == 401
    assert client.delete("/passkeys/abc", headers=OWNER, base_url=HTTPS).status_code == 404
    page = client.get("/account", headers=OWNER, base_url=HTTPS).data
    assert b'id="passkeys"' in page and b"passkey.js" in page


def test_passkeys_are_sealed_and_survive_start_over(app):
    from wyzebridge import lockdown, web_ui

    assert "passkeys.json" in {p.name for p in lockdown.secret_files()}
    assert "passkeys.json" in web_ui.KEEP_ON_START_OVER


def test_safe_next(app):
    _, _, passkeys = app
    assert passkeys.safe_next("/live?cam=a") == "/live?cam=a"
    for bad in ("//evil.example", "/\\evil.example", "https://evil.example", "", None):
        assert passkeys.safe_next(bad) == "/"
    assert base64url_to_bytes(bytes_to_base64url(b"x")) == b"x"
