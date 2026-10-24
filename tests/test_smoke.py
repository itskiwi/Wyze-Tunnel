from threading import Thread

import pytest


@pytest.fixture
def client(monkeypatch):
    # Don't log in to Wyze or start MediaMTX; only check the web app wires up.
    monkeypatch.setattr(Thread, "start", lambda self: None)
    from frontend import create_app

    return create_app().test_client()


def test_home_redirects_to_login_before_sign_in(client):
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/login")


def test_login_page_renders(client):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert b"<form" in resp.data


def test_clean_name():
    from wyzecam.api_models import clean_name

    assert clean_name("Front Door!") == "FRONT_DOOR"
    assert clean_name(" café cam ", "-") == "CAF-CAM"


def test_is_min_version():
    from wyzecam.api_models import is_min_version

    assert is_min_version("4.36.10", "4.36.9")
    assert is_min_version("4.36.9", "4.36.9")
    assert not is_min_version("4.9.1", "4.36.0")
    assert not is_min_version(None, "1.0")
