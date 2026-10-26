from datetime import time
from unittest import mock

import pytest
from wyzebridge import webhooks


@pytest.fixture(autouse=True)
def alerts_file(tmp_path, monkeypatch):
    monkeypatch.setattr(webhooks, "ALERTS_FILE", str(tmp_path / "alerts.json"))
    monkeypatch.setattr(webhooks, "ALERTS", {})


def test_save_validates_and_persists():
    with pytest.raises(ValueError):
        webhooks.save_alerts({"topic": "has spaces"})
    with pytest.raises(ValueError):
        webhooks.save_alerts({"topic": "ok", "server": "ftp://x"})
    with pytest.raises(ValueError):
        webhooks.save_alerts({"topic": "ok", "quiet_start": "22:00"})
    saved = webhooks.save_alerts({"topic": "my-topic", "motion": "on", "quiet_start": "22:00", "quiet_end": "07:00"})
    assert saved == {"server": "https://ntfy.sh", "topic": "my-topic", "chat_url": "", "quiet_start": "22:00",
                     "quiet_end": "07:00", "motion": True, "person": False, "offline": False, "summary": False, "cleared": False, "siren_away": False}
    assert webhooks.load_alerts() == saved == webhooks.ALERTS
    assert webhooks.alert_url("motion") == "https://ntfy.sh/my-topic"
    assert webhooks.alert_url("offline") == ""
    assert webhooks.alert_url("test") == "https://ntfy.sh/my-topic"


def test_quiet_hours_wrap_past_midnight():
    night = {"quiet_start": "22:00", "quiet_end": "07:00"}
    assert webhooks.in_quiet_hours(night, time(23, 30))
    assert webhooks.in_quiet_hours(night, time(6, 59))
    assert not webhooks.in_quiet_hours(night, time(7, 0))
    assert not webhooks.in_quiet_hours(night, time(12, 0))
    day = {"quiet_start": "09:00", "quiet_end": "17:00"}
    assert webhooks.in_quiet_hours(day, time(12, 0)) and not webhooks.in_quiet_hours(day, time(18, 0))
    assert not webhooks.in_quiet_hours({}, time(12, 0))


def test_send_uses_saved_topic_and_respects_quiet_hours(monkeypatch):
    webhooks.save_alerts({"topic": "t", "motion": "on", "offline": "on", "quiet_start": "00:00", "quiet_end": "23:59"})
    with mock.patch.object(webhooks.requests, "post") as post:
        assert not webhooks.send_webhook("motion", "CAM", "motion!")  # quiet hours
        assert webhooks.send_webhook("offline", "CAM", "offline!")  # quiet hours are motion only
    assert post.call_args.args[0] == "https://ntfy.sh/t"
    monkeypatch.setenv("MOTION_WEBHOOKS", "https://example.com/hook")
    with mock.patch.object(webhooks.requests, "post") as post:
        assert webhooks.send_webhook("motion", "CAM", "motion!")  # env webhooks ignore UI quiet hours
    assert post.call_args.args[0] == "https://example.com/hook"


def test_home_and_away_modes(monkeypatch):
    webhooks.save_alerts({"topic": "t", "motion": "on", "offline": "on", "quiet_start": "00:00", "quiet_end": "23:59"})
    with pytest.raises(ValueError):
        webhooks.set_alert_mode("party")
    with mock.patch.object(webhooks.requests, "post"):
        webhooks.set_alert_mode("away")
        assert webhooks.send_webhook("motion", "CAM", "motion!")  # away ignores quiet hours
        webhooks.set_alert_mode("home")
        assert not webhooks.send_webhook("motion", "CAM", "motion!")
        assert webhooks.send_webhook("offline", "CAM", "offline!")  # home still reports offline cameras
    webhooks.save_alerts({"topic": "t", "motion": "on"})
    assert webhooks.load_alerts()["mode"] == "home"  # saving the form keeps the mode


def test_siren_burst_has_cooldown(monkeypatch):
    from types import SimpleNamespace

    from wyzebridge import wyze_events

    monkeypatch.setattr(wyze_events, "SIREN_SECONDS", 0)
    monkeypatch.setattr(wyze_events, "_last_siren", {})
    stream = SimpleNamespace(uri="front", send_cmd=mock.Mock())
    with mock.patch.object(wyze_events, "Thread") as thread:
        assert wyze_events.sound_siren(stream)
        assert not wyze_events.sound_siren(stream)  # cooldown
        thread.call_args.kwargs["target"]()
    assert stream.send_cmd.call_args_list == [mock.call("alarm", "on"), mock.call("alarm", "off")]


def test_chat_links_get_their_own_message_format():
    with pytest.raises(ValueError):
        webhooks.save_alerts({"chat_url": "https://evil.example/api/webhooks/1"})
    with pytest.raises(ValueError):
        webhooks.save_alerts({"chat_url": "http://discord.com/api/webhooks/1"})
    webhooks.save_alerts({"chat_url": "https://discord.com/api/webhooks/1/x", "motion": "on"})
    with mock.patch.object(webhooks.requests, "post") as post:
        assert webhooks.send_webhook("motion", "CAM", "motion!", "https://img/1.jpg")
        assert not webhooks.send_webhook("offline", "CAM", "offline!")  # offline is off
    assert post.call_count == 1  # no ntfy topic, so only the chat
    assert post.call_args.kwargs["json"] == {"content": "**Motion Event**\nmotion!", "embeds": [{"image": {"url": "https://img/1.jpg"}}]}
    assert webhooks.chat_payload("slack", "T", "m", None) == {"text": "T: m"}
    assert webhooks.chat_service("https://api.telegram.org/bot1:a/sendMessage?chat_id=5") == "telegram"


def test_first_check_fills_the_timeline_without_alerts(monkeypatch):
    from types import SimpleNamespace

    from wyzebridge import wyze_events

    def event(i, ts, mac="AA", value="1"):
        return {"event_id": i, "event_ts": ts * 1000, "device_id": mac, "event_value": value, "tag_list": [101],
                "file_list": [{"type": 1, "url": f"https://img/{i}.jpg"}]}

    api = mock.Mock()
    api.get_events.return_value = (5.0, [event("b", 200, value="10"), event("x", 150, mac="ZZ"), event("a", 100)])
    cam = SimpleNamespace(mac="AA", nickname="Front Door", name_uri="front")
    stream = SimpleNamespace(api=api, camera=cam, uri="front", enabled=True, options=SimpleNamespace(substream=False))
    log = wyze_events.deque([{"uri": "front", "ts": 300, "label": "person"}], maxlen=3)  # a newer local detection
    monkeypatch.setattr(wyze_events, "EVENT_LOG", log)
    monkeypatch.setattr(wyze_events, "display_name", lambda c: c.nickname)
    with mock.patch.object(wyze_events, "send_webhook") as hook:
        events = wyze_events.WyzeEvents({"front": stream})
        events.check_motion()
    assert api.get_events.call_args.kwargs == {"history": 86400}
    assert [e["ts"] for e in log] == [300, 200, 100]  # newest first, unknown cam skipped
    assert log[1]["label"] == "rang the doorbell" and log[2]["label"] == "person"
    assert (events.last_ts, events.last_check) == (200, 5.0)  # next poll picks up after the backfill
    hook.assert_not_called()
    api.save_thumbnail.assert_not_called()
