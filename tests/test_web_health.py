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
    assert "isn&#39;t reachable" in page or "isn't reachable" in page
    assert "SD card is almost full" not in page
