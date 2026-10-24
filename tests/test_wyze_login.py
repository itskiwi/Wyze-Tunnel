import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("TOKEN_PATH", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import wyzecam  # noqa: E402
from wyzebridge import wyze_api  # noqa: E402
from wyzecam.api import WyzeAPIError  # noqa: E402
from wyzecam.api_models import WyzeCredential  # noqa: E402


def make_api(monkeypatch, login):
    monkeypatch.setattr(wyze_api.WyzeApi, "auth_locked", property(lambda self: False))
    monkeypatch.setattr(wyzecam, "login", login)
    api = wyze_api.WyzeApi()
    api.creds.update("me@example.com", "pw", "k" * 36, "a" * 60)
    return api


def test_2fa_code_from_wizard_completes_login(monkeypatch):
    calls = []

    def login(**kw):
        calls.append(kw)
        if kw["mfa"]:
            return WyzeCredential(access_token="tok", phone_id=kw["phone_id"])
        return WyzeCredential(mfa_options=["TotpVerificationCode"], mfa_details={"totp_apps": [{"app_id": "app1"}]}, phone_id="p1")

    api = make_api(monkeypatch, login)
    t = threading.Thread(target=api.attempt_login, args=(True,))
    t.start()
    while not api.mfa:
        time.sleep(0.05)
    assert api.mfa == {"mfa_type": "TotpVerificationCode", "verification_id": "app1"}
    api.mfa["verification_code"] = "123456"  # what POST /login mfaCode does
    t.join(5)

    assert api.auth.access_token == "tok"
    assert calls[1]["phone_id"] == "p1"
    assert calls[1]["mfa"]["verification_code"] == "123456"
    assert api.mfa is None and api.login_error == ""


def test_wizard_login_error_is_reported_and_creds_cleared(monkeypatch):
    class Req:
        method, path_url = "POST", "/api/user/login"

    def login(**kw):
        raise WyzeAPIError("1001", "Invalid password", Req)

    api = make_api(monkeypatch, login)
    api.attempt_login(web=True)
    assert api.auth is None
    assert api.login_error == "Invalid password"
    assert not api.creds.is_set


def test_status_reveals_web_password_only_to_the_wizard_browser(monkeypatch):
    import frontend
    from wyzebridge.auth import WbAuth

    class FakeBridge:
        api = wyze_api.WyzeApi()

        def start(self):
            pass

    monkeypatch.setattr(frontend, "WyzeBridge", FakeBridge)
    monkeypatch.setattr(WbAuth, "enabled", True)
    monkeypatch.setattr(WbAuth, "api", "")
    monkeypatch.setattr(WbAuth, "_pass", "s3cret")
    app = frontend.create_app()
    client = app.test_client()

    assert client.get("/login/status").json == {"state": "idle"}
    form = {"email": "me@example.com", "password": "pw", "keyId": "k" * 36, "apiKey": "a" * 60}
    token = client.post("/login", data=form).json["setup"]
    assert client.get("/login/status").json == {"state": "signing_in"}

    FakeBridge.api.auth = WyzeCredential(access_token="tok")
    assert client.get("/login/status").json == {"state": "signing_in"}  # web password not set up yet
    monkeypatch.setattr(WbAuth, "api", "wbapi")

    assert client.get("/login/status?setup=wrong").json == {"state": "done"}
    shown = client.get(f"/login/status?setup={token}").json
    assert shown == {"state": "done", "username": WbAuth.username, "password": "s3cret"}
    assert "password" not in client.get(f"/login/status?setup={token}").json  # only once
