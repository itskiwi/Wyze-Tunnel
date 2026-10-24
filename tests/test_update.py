import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

os.environ["TOKEN_PATH"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).parents[1] / "app"))


def make_client(monkeypatch, flag, auth=False):
    import frontend
    from wyzebridge import web_ui

    monkeypatch.setattr(web_ui.WbAuth, "enabled", auth)
    if flag:
        monkeypatch.setenv("WT_UPDATE_FILE", str(flag))
    else:
        monkeypatch.delenv("WT_UPDATE_FILE", raising=False)
    with mock.patch.object(frontend, "WyzeBridge"):
        return frontend.create_app().test_client()


def test_update_button_writes_flag_for_native_installs(monkeypatch, tmp_path):
    flag = tmp_path / "update-now"
    client = make_client(monkeypatch, flag)
    assert client.post("/update").json == {"result": "ok"}
    assert flag.exists()


def test_update_refused_without_native_updater(monkeypatch):
    client = make_client(monkeypatch, None)
    assert client.post("/update").status_code == 400


def test_update_needs_auth_and_same_site(monkeypatch, tmp_path):
    flag = tmp_path / "update-now"
    assert make_client(monkeypatch, flag, auth=True).post("/update").status_code == 401
    # The API key only opens the camera API, not updates.
    monkeypatch.setattr("wyzebridge.web_ui.WbAuth.api", "key")
    assert make_client(monkeypatch, flag, auth=True).post("/update?api=key").status_code == 401
    client = make_client(monkeypatch, flag)
    assert client.post("/update", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert not flag.exists()
