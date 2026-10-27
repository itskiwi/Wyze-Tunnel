# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
from base64 import b64encode
from types import SimpleNamespace
from unittest import mock

import pytest
from werkzeug.security import generate_password_hash


@pytest.fixture
def client(monkeypatch, tmp_path):
    import frontend
    from wyzebridge import guests, mtx_server, webhooks, web_ui

    monkeypatch.setattr(guests, "GUESTS_FILE", str(tmp_path / "guests.json"))
    monkeypatch.setattr(mtx_server, "MTX_CONFIG", str(tmp_path / "mediamtx.yml"))
    monkeypatch.setattr(webhooks, "ALERTS_FILE", str(tmp_path / "alerts.json"))
    monkeypatch.setattr(webhooks, "ALERTS", {})
    for cls in {web_ui.WbAuth, frontend.WbAuth}:
        monkeypatch.setattr(cls, "enabled", True)
        monkeypatch.setattr(cls, "username", "owner")
        monkeypatch.setattr(cls, "_hashed_pass", generate_password_hash("ownerpass"))
    with mock.patch.object(frontend, "WyzeBridge"):
        app = frontend.create_app()
    c = app.test_client()
    c.environ_base["HTTP_AUTHORIZATION"] = "Basic " + b64encode(b"owner:ownerpass").decode()
    return c


def test_settings_owner_only_and_glass_on_by_default(client):
    from wyzebridge import guests

    assert b'name="glass" class="mt-1 h-4 w-4 accent-accent" checked' in client.get("/alerts").data
    for hours in (1, 0):  # a watch-only guest, then family
        password = guests.add_guest(f"Guest{hours}", hours, "owner")
        guest = {"Authorization": "Basic " + b64encode(f"Guest{hours}:{password}".encode()).decode()}
        assert client.post("/alerts", json={"topic": "x", "crying": True}, headers=guest).status_code == 403
    assert client.post("/alerts", json={"topic": "x", "crying": True}).status_code == 200
    from wyzebridge import webhooks

    assert webhooks.ALERTS["crying"] is True and webhooks.ALERTS["glass"] is False  # saved form wins


def test_urgent_ignores_switches_and_quiet_hours(monkeypatch):
    from wyzebridge import webhooks

    monkeypatch.setattr(webhooks, "ALERTS", {"server": "https://ntfy.sh", "topic": "t", "quiet_start": "00:00", "quiet_end": "23:59", "motion": False})
    monkeypatch.setattr(webhooks, "integrations", mock.Mock())
    monkeypatch.setattr(webhooks, "_post", lambda url, event, cam, msg, img, title: sent.append((event, title)) or True)
    sent = []
    assert webhooks.send_webhook("smoke", "cam", "A smoke alarm heard", title="Smoke alarm: Hall")
    assert webhooks.send_webhook("co", "cam", "x")
    assert webhooks.send_webhook("glass", "cam", "x")  # default on
    assert not webhooks.send_webhook("crying", "cam", "x")  # opt-in
    assert sent[1] == ("co", "Carbon monoxide alarm")


def test_events_map_to_labels_and_webhooks(monkeypatch):
    from wyzebridge import wyze_events

    def event(value, tags):
        return {"event_id": "e", "event_ts": 1, "device_id": "AA", "event_value": value, "tag_list": tags, "file_list": []}

    stream = SimpleNamespace(camera=SimpleNamespace(mac="AA", nickname="Hall", name_uri="hall"), uri="hall", options=SimpleNamespace(substream=False),
                             motion=0, start=mock.Mock())
    monkeypatch.setattr(wyze_events, "EVENT_LOG", wyze_events.deque(maxlen=10))
    monkeypatch.setattr(wyze_events, "display_name", lambda c: c.nickname)
    monkeypatch.setattr(wyze_events, "MOTION_START", False)
    ev = wyze_events.WyzeEvents.__new__(wyze_events.WyzeEvents)
    ev.streams, ev.api, ev.last_ts = {"hall": stream}, mock.Mock(), 1
    with mock.patch.object(wyze_events, "send_webhook") as hook:
        for e in (event("4", []), event("5", [101]), event("1", [800006]), event("2", [800001]), event("1", [101])):
            ev.set_motion(*ev.parse(e))
    sent = [(c.args[0], c.kwargs["title"]) for c in hook.call_args_list]
    assert sent == [("smoke", "Smoke alarm: Hall"), ("co", "Carbon monoxide alarm: Hall"), ("glass", "Hall"), ("crying", "Hall"), ("motion", "Hall")]
    assert [e.get("label") for e in reversed(wyze_events.EVENT_LOG)] == ["smoke", "co", "glass", "crying", "person"]
    assert wyze_events.EVENT_LOG[-1]["text"] == "A smoke alarm heard on Hall"
