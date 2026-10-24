# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
from unittest import mock

import pytest

CAMS = {
    "front": {"status": 3, "enabled": True, "motion": False, "net_mode": "local",
              "camera_info": {"basicInfo": {"wifidb": "72"}}},
    "back": {"status": -90, "enabled": True, "motion": False, "net_mode": "",
             "camera_info": {"basicInfo": {"wifidb": "20"}, "sdParm": {"status": "1", "capacity": "100", "free": "5"}}},
}


@pytest.fixture
def client(monkeypatch):
    import frontend
    from wyzebridge import web_ui

    monkeypatch.setattr(web_ui.auth, "verify_password_callback", lambda user, password: True)
    with mock.patch.object(frontend, "WyzeBridge"), \
            mock.patch.object(web_ui, "all_cams", return_value={"cameras": CAMS}):
        yield frontend.create_app().test_client()


def test_api_health_reports_degraded(client):
    data = client.get("/api/health").json
    assert data["status"] == "degraded" and data["cameras"] == 2
    assert {i["issue"] for i in data["issues"]} == {"offline", "weak_wifi", "sd_card_full"}
    assert {i["camera"] for i in data["issues"]} == {"back"}


def test_metrics_prom(client):
    from wyzecam import api

    api.API_STATS.update(ok=5, error=1)
    body = client.get("/metrics.prom").get_data(as_text=True)
    assert 'wyze_camera_streaming{camera="front"} 1' in body
    assert 'wyze_camera_offline{camera="back"} 1' in body
    assert 'wyze_camera_wifi_percent{camera="front"} 72' in body
    assert 'wyze_api_calls_total{result="ok"} 5' in body


def test_validate_resp_counts_calls():
    from wyzecam import api

    api.API_STATS.clear()
    ok = mock.Mock(headers={"X-RateLimit-Remaining": "50"}, json=lambda: {"code": "1", "data": {"a": 1}})
    assert api.validate_resp(ok) == {"a": 1}
    bad = mock.Mock(headers={}, json=lambda: {"code": "1005", "msg": "nope"})
    with pytest.raises(api.WyzeAPIError):
        api.validate_resp(bad)
    assert api.API_STATS["ok"] == 1 and api.API_STATS["error"] == 1
    assert api.API_STATS["rate_limit_remaining"] == 100
