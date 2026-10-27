# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
from base64 import b64encode
from unittest import mock

import pytest
from werkzeug.security import generate_password_hash


@pytest.fixture
def client(monkeypatch, tmp_path):
    import frontend
    from wyzebridge import transcripts, web_ui

    monkeypatch.setattr(transcripts, "SETTINGS_FILE", str(tmp_path / "transcripts.json"))
    monkeypatch.setattr(transcripts, "available", lambda: True)
    for cls in {web_ui.WbAuth, frontend.WbAuth}:
        monkeypatch.setattr(cls, "enabled", True)
        monkeypatch.setattr(cls, "username", "owner")
        monkeypatch.setattr(cls, "_hashed_pass", generate_password_hash("ownerpass"))
    with mock.patch.object(frontend, "WyzeBridge"), mock.patch.object(frontend, "Thread") as thread:
        c = frontend.create_app().test_client()
        c.environ_base["HTTP_AUTHORIZATION"] = "Basic " + b64encode(b"owner:ownerpass").decode()
        yield transcripts, thread, c


def test_turning_on_needs_the_consent_box(client):
    transcripts, thread, c = client
    assert 'id="transcriptsOn"' in c.get("/alerts").data.decode()
    assert c.post("/alerts/transcripts", json={"on": True}).status_code == 400
    assert not transcripts.enabled()
    assert c.post("/alerts/transcripts", json={"on": True, "agreed": True}).json == {"status": "saved", "on": True}
    assert transcripts.enabled() and thread.return_value.start.called
