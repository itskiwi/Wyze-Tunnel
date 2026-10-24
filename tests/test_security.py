import importlib
import os
import sys
import tempfile
from pathlib import Path

os.environ["TOKEN_PATH"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).parents[1] / "app"))

from flask import Flask  # noqa: E402
from wyzebridge import auth, web_ui  # noqa: E402

EMAIL = "john123@doe.com"


def fresh_auth(tmp_path, legacy: bool):
    os.environ["TOKEN_PATH"] = str(tmp_path)
    if legacy:
        (tmp_path / "user.pickle").write_bytes(b"x")
    import wyzebridge.config

    importlib.reload(wyzebridge.config)
    mod = importlib.reload(auth)
    mod.WbAuth.set_email(EMAIL)
    return mod


def test_new_install_gets_random_saved_credentials(tmp_path):
    mod = fresh_auth(tmp_path, legacy=False)
    password, api = mod.WbAuth._pass, mod.WbAuth.api
    assert password != "john123" and len(password) >= 20
    assert api != mod.gen_api_key(EMAIL) and len(api) >= 40
    assert (tmp_path / "wb_password").read_text() == password
    assert oct((tmp_path / "wb_api").stat().st_mode)[-3:] == "600"

    mod.WbAuth.set_email(EMAIL)  # restart keeps them
    assert (mod.WbAuth._pass, mod.WbAuth.api) == (password, api)


def test_existing_install_keeps_old_credentials(tmp_path):
    mod = fresh_auth(tmp_path, legacy=True)
    assert mod.WbAuth._pass == "john123"
    assert mod.WbAuth.api == mod.gen_api_key(EMAIL)


def cross_site(**headers):
    with Flask(__name__).test_request_context("/restart/all", headers=headers):
        return web_ui.cross_site_request()


def test_cross_site_detection():
    assert cross_site(**{"Sec-Fetch-Site": "cross-site"})
    assert cross_site(**{"Sec-Fetch-Site": "same-site"})
    assert not cross_site(**{"Sec-Fetch-Site": "same-origin"})
    assert not cross_site(**{"Sec-Fetch-Site": "none"})
    assert cross_site(Origin="http://evil.example")
    assert not cross_site(Origin="http://localhost")
    assert not cross_site()  # curl, Home Assistant, scripts
