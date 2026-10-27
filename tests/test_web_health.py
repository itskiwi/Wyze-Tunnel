from unittest import mock

import pytest


@pytest.fixture
def client(monkeypatch):
    import frontend
    from wyzebridge import web_ui

    monkeypatch.setattr(web_ui.auth, "verify_password_callback", lambda user, password: True)
    cams = {
        "weak": {"nickname": "Weak", "status": 3, "enabled": True, "start_time": 0,
                 "camera_info": {"basicInfo": {"wifidb": "30"}, "sdParm": {"status": "1", "capacity": "100", "free": "50"}}},
        "gone": {"nickname": "Gone", "status": -90, "enabled": True},
    }
    monkeypatch.setattr(web_ui, "all_cams", lambda *a: {"cameras": cams})
    with mock.patch.object(frontend, "WyzeBridge"):
        return frontend.create_app().test_client()


def test_health_gives_plain_english_fixes(client):
    page = client.get("/health").get_data(as_text=True)
    assert "Weak Wi-Fi (30%)" in page and "50%" in page
    assert '<span class="chip chip-bad">Weak</span>' in page  # the Good / Fair / Weak badge
    assert "isn&#39;t reachable" in page or "isn't reachable" in page
    assert "SD card is almost full" not in page


def test_home_timeline_lists_recent_motion(client):
    from wyzebridge.wyze_events import EVENT_LOG

    assert "Motion is off" in client.get("/").get_data(as_text=True)
    EVENT_LOG.appendleft({"uri": "front", "name": "Front Door", "ts": 1761300000, "img": "https://example.com/a.jpg"})
    try:
        page = client.get("/").get_data(as_text=True)
    finally:
        EVENT_LOG.clear()
    assert '"name": "Front Door"' in page and "https://example.com/a.jpg" in page and "1761300000" in page
    assert ">Weak<" in page and "1 offline" in page  # camera strip and status chips
    assert client.get("/events").headers["Location"] == "./"


def test_grid_links_and_wall_displays_open_live_view(client):
    assert client.get("/?columns=3&fullscreen").headers["Location"] == "live?columns=3&fullscreen"
    client.set_cookie("fullscreen", "1")
    assert client.get("/").headers["Location"] == "live"


def test_live_view_survives_partial_camera_info(client):
    page = client.get("/live").get_data(as_text=True)
    assert "Weak" in page  # camera_info without channelResquestResult used to crash the page
    assert "Camera offline" in page  # instead of a spinner that never ends
    assert "cdnjs" not in page and "jsdelivr" not in page  # icons and video work without internet


def test_pages_use_plain_words(client):
    live = client.get("/live").get_data(as_text=True)
    assert "<title>Live · Wyze Tunnel</title>" in live
    assert ">idle<" in live and "stopped" not in live
    assert "Account and sharing" in live and "Wyze login" not in live


def test_menu_is_simple_until_advanced_is_on(client):
    page = client.get("/health").get_data(as_text=True)
    assert "Choose cameras" in page and "Show advanced" in page
    assert 'href="network"' not in page and 'href="apps"' not in page
    client.set_cookie("advanced", "1")
    page = client.get("/health").get_data(as_text=True)
    assert 'href="network"' in page and 'href="apps"' in page and "Show fewer options" in page


def test_wifi_tiers():
    from wyzebridge.web_ui import wifi_tier

    assert [wifi_tier(v) for v in ("75", 60, 59, 40, 39, 0, None, "?")] == ["Good", "Good", "Fair", "Fair", "Weak", "Weak", "", ""]


def test_live_shows_wifi_tier(client):
    assert 'aria-label="Wi-Fi: Weak"' in client.get("/live").get_data(as_text=True)
