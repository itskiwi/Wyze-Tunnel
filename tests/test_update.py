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


def test_update_status_reports_running_version(monkeypatch, tmp_path):
    from wyzebridge import config

    client = make_client(monkeypatch, tmp_path / "update-now")
    assert client.get("/update").json == {"version": config.VERSION, "can_update": True}
    assert make_client(monkeypatch, None).get("/update").json["can_update"] is False


def test_viewer_count_for_nightly_updater(monkeypatch, tmp_path):
    from wyzebridge import mtx_event

    monkeypatch.setenv("WT_UPDATE_FILE", str(tmp_path / "update-now"))
    monkeypatch.setattr(mtx_event, "VIEWERS", {})
    mtx_event.read_event("front", "read")
    mtx_event.read_event("back", "read")
    assert (tmp_path / "viewers").read_text() == "2"
    mtx_event.read_event("front", "unread")
    mtx_event.read_event("front", "unread")
    assert (tmp_path / "viewers").read_text() == "1"


def test_update_alert_sent_once_per_new_version(monkeypatch, tmp_path):
    from wyzebridge import webhooks

    sent = []
    monkeypatch.setattr(webhooks, "TOKEN_PATH", str(tmp_path))
    monkeypatch.setattr(webhooks, "send_webhook", lambda *a: sent.append(a))
    body = "## What's Changed\n* Fix things by @kiwi in https://github.com/x/y/pull/1\n* More by @kiwi in https://x/2\n"
    monkeypatch.setattr(webhooks.requests, "get", lambda *a, **k: mock.Mock(json=lambda: {"body": body}))
    webhooks.announce_update()  # first start: nothing to compare with
    assert sent == []
    (tmp_path / "last_version").write_text("0.0.1")
    webhooks.announce_update()
    webhooks.announce_update()
    assert len(sent) == 1
    assert sent[0][0] == "update"
    assert f"v{webhooks.VERSION} (was v0.0.1)" in sent[0][2]
    assert "• Fix things\n• More" in sent[0][2]
