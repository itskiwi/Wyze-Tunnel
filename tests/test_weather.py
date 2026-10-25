import json
from unittest import mock

import pytest
import requests

from wyzebridge import weather

# Made-up spot and town.
IP = {"latitude": 40.123456, "longitude": -100.987654, "city": "Sampleton", "country_code": "US"}
FORECAST = {"current": {"temperature_2m": 54.4, "weather_code": 2, "is_day": 0},
            "daily": {"sunrise": [1774000000], "sunset": [1774043000]}}


def reply(body):
    return mock.Mock(json=lambda: body, raise_for_status=lambda: None)


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(weather, "LOCATION_FILE", str(tmp_path / "location.json"))
    monkeypatch.setattr(weather, "_guessed", {"at": 0.0})
    monkeypatch.setattr(weather, "_cache", {})


def test_guess_is_rounded_and_saved_once():
    with mock.patch.object(weather.requests, "get", return_value=reply(IP)) as get:
        assert weather.location() == {"lat": 40.12, "lon": -100.99, "name": "Sampleton", "unit": "°F"}
        assert weather.location()["lat"] == 40.12
    assert get.call_count == 1
    assert json.loads(open(weather.LOCATION_FILE).read())["lon"] == -100.99


def test_failed_guess_returns_none_and_waits_an_hour():
    with mock.patch.object(weather.requests, "get", side_effect=requests.ConnectionError) as get:
        assert weather.location() is None
        assert weather.location() is None
        assert weather.current() is None
    assert get.call_count == 1


def test_current_weather_is_cached_and_survives_going_offline():
    with mock.patch.object(weather.requests, "get", side_effect=[reply(IP), reply(FORECAST)]) as get:
        now = weather.current()
        assert weather.current() == now
    assert get.call_count == 2
    assert now["temp"] == 54 and now["unit"] == "°F" and now["text"] == "Partly cloudy"
    assert now["icon"] == "fa-cloud-moon" and now["is_day"] is False and now["sunrise"] == 1774000000
    assert "temperature_unit=fahrenheit" in "&".join(f"{k}={v}" for k, v in get.call_args.kwargs["params"].items())
    weather._cache["at"] = 0  # 30 minutes later, and offline
    with mock.patch.object(weather.requests, "get", side_effect=requests.Timeout):
        assert weather.current() == now


def test_typed_town_is_looked_up():
    hit = {"results": [{"latitude": 51.507, "longitude": 7.123, "name": "Exampleburg", "country_code": "DE"}]}
    with mock.patch.object(weather.requests, "get", return_value=reply(hit)):
        loc = weather.save(True, "exampleburg")
    assert loc == {"lat": 51.51, "lon": 7.12, "name": "Exampleburg", "unit": "°C", "weather": True}
    assert weather.enabled()
    with mock.patch.object(weather.requests, "get", return_value=reply({})), pytest.raises(ValueError):
        weather.save(True, "Nowhere at all")
    with mock.patch.object(weather.requests, "get") as get:
        weather.save(False, "Exampleburg")  # same town: no lookup
    assert not get.called and not weather.enabled()


def test_weather_route_is_empty_when_offline(monkeypatch):
    import frontend
    from wyzebridge import web_ui

    monkeypatch.setattr(web_ui.auth, "verify_password_callback", lambda user, password: True)
    with mock.patch.object(frontend, "WyzeBridge"), mock.patch.object(frontend, "Thread"):
        client = frontend.create_app().test_client()
    with mock.patch.object(weather.requests, "get", side_effect=requests.ConnectionError):
        assert client.get("/weather").json == {}
    assert 'name="weather"' in client.get("/account").text

    cams = {"yard": {"nickname": "Yard", "enabled": True, "outdoor": True}, "hall": {"nickname": "Hall", "enabled": True}}
    monkeypatch.setattr(web_ui, "all_cams", lambda *_: {"enabled": 2, "available": 2, "total": 2, "cameras": cams})
    assert "weather-tag" not in client.get("/live").text  # off by default
    weather.save(True, "")
    assert client.get("/live").text.count('class="weather-tag') == 1


def test_location_is_in_the_settings_backup():
    from wyzebridge import backup

    with mock.patch.object(weather.requests, "get", return_value=reply(IP)):
        weather.location()
    saved = backup.export_settings()
    assert saved["settings"]["location"]["lat"] == 40.12
    assert "location" in backup.import_settings(saved)
    saved["settings"]["location"] = {"lat": "here"}
    with pytest.raises(ValueError):
        backup.import_settings(saved)
