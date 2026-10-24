# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
from unittest import mock

import pytest


@pytest.fixture
def setup(monkeypatch, tmp_path):
    import frontend
    from wyzebridge import web_ui, webhooks, wyze_api, wyze_stream

    for mod, attr in [(webhooks, "ALERTS_FILE"), (wyze_api, "HIDDEN_CAMS_FILE"), (wyze_api, "CAM_NAMES_FILE"),
                      (wyze_stream, "NET_MODES_FILE"), (wyze_stream, "QUALITY_FILE")]:
        monkeypatch.setattr(mod, attr, str(tmp_path / f"{attr}.json"))
    monkeypatch.setattr(webhooks, "ALERTS", {})
    monkeypatch.setattr(web_ui.auth, "verify_password_callback", lambda user, password: True)
    with mock.patch.object(frontend, "WyzeBridge"), mock.patch.object(frontend, "Thread") as thread:
        yield frontend.create_app().test_client(), wyze_api, webhooks, thread


def test_backup_round_trip(setup):
    client, wyze_api, webhooks, thread = setup
    webhooks.save_alerts({"topic": "t", "motion": "on"})
    webhooks.set_alert_mode("away")
    wyze_api.save_cam_name("AA", "Porch")
    wyze_api.save_hidden_cams({"BB"})
    resp = client.get("/settings/backup")
    assert "attachment" in resp.headers["Content-Disposition"]
    saved = resp.json
    assert set(saved["settings"]) == {"alerts", "cam_names", "hidden_cams"}

    wyze_api.save_cam_name("AA", "")
    webhooks.set_alert_mode("home")
    assert client.post("/settings/restore", json=saved).json["status"] == "restored"
    assert wyze_api.load_cam_names() == {"AA": "Porch"}
    assert webhooks.ALERTS["mode"] == "away"
    assert thread.return_value.start.called


def test_restore_rejects_bad_files(setup):
    client, wyze_api, _, _ = setup
    assert client.post("/settings/restore", json={"foo": 1}).status_code == 400
    bad = {"wyze_tunnel_backup": 1, "settings": {"cam_names": {"AA": "x"}, "quality": {"front": "8k"}}}
    assert client.post("/settings/restore", json=bad).status_code == 400
    assert wyze_api.load_cam_names() == {}  # nothing written when any part is bad
    tokens = {"wyze_tunnel_backup": 1, "settings": {"auth": {"token": "x"}}}
    assert client.post("/settings/restore", json=tokens).status_code == 400


def test_locked_backup_round_trip(setup):
    client, wyze_api, _, _ = setup
    wyze_api.save_cam_name("AA", "Porch")
    assert client.post("/settings/backup", json={"passphrase": "short"}).status_code == 400
    locked = client.post("/settings/backup", json={"passphrase": "correct horse"}).json
    assert "Porch" not in str(locked) and "settings" not in locked

    wyze_api.save_cam_name("AA", "")
    assert "locked" in client.post("/settings/restore", json=locked).json["error"]
    assert "doesn't open" in client.post("/settings/restore", json={**locked, "passphrase": "wrong one"}).json["error"]
    assert client.post("/settings/restore", json={**locked, "passphrase": "correct horse"}).json["status"] == "restored"
    assert wyze_api.load_cam_names() == {"AA": "Porch"}
