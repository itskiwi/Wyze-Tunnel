from datetime import datetime

from wyzebridge import summary


def test_compose_counts_the_night(monkeypatch):
    monkeypatch.setattr(summary, "ALERTS", {})
    since, until = summary.window(datetime(2026, 10, 25, 7, 30))
    assert (since, until) == (datetime(2026, 10, 24, 22), datetime(2026, 10, 25, 7))
    at = lambda h, d=25: datetime(2026, 10, d, h).timestamp()
    events = [
        {"name": "Front", "ts": at(2)},
        {"name": "Front", "ts": at(3)},
        {"name": "Door", "ts": at(23, 24), "label": "rang the doorbell"},
        {"name": "Back", "ts": at(12)},  # daytime, not counted
    ]
    assert summary.compose(events, since, until) == "From 22:00 to 07:00: motion 2 times (2 on Front) and the doorbell rang once."
    assert summary.compose([], since, until) == "A quiet night. Nothing was seen from 22:00 to 07:00."


def test_sent_once_in_the_morning(monkeypatch):
    sent = []
    monkeypatch.setattr(summary, "ALERTS", {"summary": True, "quiet_start": "23:00", "quiet_end": "06:30"})
    monkeypatch.setattr(summary, "send_webhook", lambda *a: sent.append(a) or True)
    monkeypatch.setitem(summary._sent_on, "day", None)
    assert not summary.check([], datetime(2026, 10, 25, 6, 0))
    assert summary.check([], datetime(2026, 10, 25, 6, 31))
    assert not summary.check([], datetime(2026, 10, 25, 6, 40))
    monkeypatch.setitem(summary._sent_on, "day", None)
    assert not summary.check([], datetime(2026, 10, 25, 12, 0))  # restarted at noon: too late to send
    assert len(sent) == 1 and sent[0][0] == "summary"
