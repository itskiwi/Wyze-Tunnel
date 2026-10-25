from wyzebridge import backup, guests, recording, web_ui, webhooks


def test_steps_follow_settings(monkeypatch):
    monkeypatch.setattr(recording, "load_settings", lambda: {"cams": ["front"]})
    monkeypatch.setattr(guests, "list_guests", lambda: [{"name": "iPhone", "expires": 0}])
    monkeypatch.setattr(backup, "load_auto", lambda: {})
    monkeypatch.setattr(webhooks, "ALERTS", {})
    steps = {s["title"]: s["done"] for s in web_ui.setup_steps({"front": {"connected": True}})}
    assert steps == {"Watch a camera": True, "Get alerts on your phone": False, "Record a camera": True,
                     "Pair your phone": True, "Back up your settings every day": False}
