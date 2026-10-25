"""Home location and the weather outside, from free services with no key (ipapi.co and Open-Meteo). Never raises."""
import json
import time

import requests

from wyzebridge.config import TOKEN_PATH
from wyzebridge.logging import logger

LOCATION_FILE = TOKEN_PATH + "location.json"
FAHRENHEIT = {"US", "LR", "MM"}
# Highest WMO weather code in each group, its words and its Font Awesome icon.
CODES = [(0, "Clear", "fa-sun"), (2, "Partly cloudy", "fa-cloud-sun"), (3, "Cloudy", "fa-cloud"), (48, "Fog", "fa-smog"),
         (57, "Drizzle", "fa-cloud-rain"), (67, "Rain", "fa-cloud-rain"), (77, "Snow", "fa-snowflake"),
         (82, "Showers", "fa-cloud-showers-heavy"), (86, "Snow", "fa-snowflake"), (99, "Thunderstorm", "fa-cloud-bolt")]
_guessed = {"at": 0.0}
_cache: dict = {}


def load() -> dict:
    try:
        with open(LOCATION_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _save(data: dict) -> None:
    with open(LOCATION_FILE, "w") as f:
        json.dump(data, f)


def _place(lat, lon, name, country) -> dict:
    # Two decimals is about 1 km: enough for the weather, not enough to find the house.
    return {"lat": round(float(lat), 2), "lon": round(float(lon), 2), "name": str(name or "")[:80],
            "unit": "°F" if country in FAHRENHEIT else "°C"}


def location() -> dict | None:
    """The saved spot, or a one-time guess from the public IP, retried at most once an hour."""
    loc = load()
    if "lat" in loc:
        return loc
    if time.time() - _guessed["at"] < 3600:
        return None
    _guessed["at"] = time.time()
    try:
        resp = requests.get("https://ipapi.co/json/", timeout=5)
        resp.raise_for_status()
        j = resp.json()
        loc |= _place(j["latitude"], j["longitude"], j.get("city"), j.get("country_code"))
    except (requests.RequestException, KeyError, TypeError, ValueError) as ex:
        logger.debug(f"[WEATHER] Couldn't guess the location: {ex}")
        return None
    _save(loc)
    return loc


def enabled() -> bool:
    return load().get("weather") is True


def save(on, town) -> dict:
    """Save the weather toggle, and look up the town if it changed. ValueError if the town isn't found."""
    loc, town = load(), str(town or "").strip()[:80]
    if town and town != loc.get("name"):
        try:
            resp = requests.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": town, "count": 1}, timeout=5)
            hit = resp.json()["results"][0]
            loc |= _place(hit["latitude"], hit["longitude"], hit.get("name") or town, hit.get("country_code"))
        except requests.RequestException:
            raise ValueError("Couldn't look up the town. Check the internet connection and try again.") from None
        except (KeyError, IndexError, TypeError, ValueError):
            raise ValueError(f"Couldn't find {town}. Try the nearest bigger town.") from None
    loc["weather"] = bool(on)
    _save(loc)
    return loc


def current() -> dict | None:
    """Weather now plus today's sunrise and sunset (Unix seconds). Cached 30 minutes; the last good value if offline."""
    if not (loc := location()):
        return _cache.get("value")
    key = (loc["lat"], loc["lon"], loc.get("unit"))
    if _cache.get("key") == key and time.time() - _cache.get("at", 0) < 1800:
        return _cache.get("value")
    _cache.update(key=key, at=time.time() - 1500)  # a failed try waits 5 minutes, so pages don't each wait on a dead link
    try:
        params = {"latitude": loc["lat"], "longitude": loc["lon"], "current": "temperature_2m,weather_code,is_day",
                  "daily": "sunrise,sunset", "timezone": "auto", "timeformat": "unixtime", "forecast_days": 1}
        if loc.get("unit") == "°F":
            params["temperature_unit"] = "fahrenheit"
        resp = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=5)
        resp.raise_for_status()
        j = resp.json()
        now, day = j["current"], j["daily"]
        is_day = bool(now["is_day"])
        text, icon = next(((t, i) for top, t, i in CODES if int(now["weather_code"]) <= top), ("Cloudy", "fa-cloud"))
        if not is_day:
            icon = {"fa-sun": "fa-moon", "fa-cloud-sun": "fa-cloud-moon"}.get(icon, icon)
        value = {"temp": round(now["temperature_2m"]), "unit": loc.get("unit", "°C"), "text": text, "icon": icon,
                 "is_day": is_day, "sunrise": int(day["sunrise"][0]), "sunset": int(day["sunset"][0]),
                 "lat": loc["lat"], "lon": loc["lon"], "name": loc.get("name", "")}
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as ex:
        logger.debug(f"[WEATHER] Couldn't get the weather: {ex}")
        return _cache.get("value")
    _cache.update(at=time.time(), value=value)
    return value
