# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
import json
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


def test_daily_locked_backup(setup, monkeypatch, tmp_path):
    client, wyze_api, _, _ = setup
    from wyzebridge import backup

    monkeypatch.setattr(backup, "AUTO_FILE", str(tmp_path / "auto.json"))
    folder = tmp_path / "nas"
    assert "already exists" in client.post("/settings/auto-backup", json={"folder": str(folder), "passphrase": "correct horse"}).json["error"]
    folder.mkdir()
    wyze_api.save_cam_name("AA", "Porch")
    path = client.post("/settings/auto-backup", json={"folder": str(folder), "passphrase": "correct horse"}).json["path"]
    assert "Porch" not in open(path).read()
    assert backup.auto_backup() is None  # not due again until tomorrow

    day = backup.load_auto()["last"]
    for i in range(1, 10):
        assert backup.auto_backup(now=day + i * 86400)
    assert len(list(folder.glob("wyze-tunnel-settings-*.json"))) == backup.AUTO_KEEP
    locked = json.loads(sorted(folder.iterdir())[-1].read_text())
    assert backup.unlock({**locked, "passphrase": "correct horse"})["settings"]["cam_names"] == {"AA": "Porch"}

    # Saving again without a passphrase keeps the stored one; an empty folder turns it off.
    assert client.post("/settings/auto-backup", json={"folder": str(folder)}).json["path"]
    assert client.post("/settings/auto-backup", json={"folder": ""}).json["path"] is None
    assert backup.auto_backup(force=True) is None
