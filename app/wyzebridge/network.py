"""Every outbound connection Wyze Tunnel makes, for the Network page.

HTTP calls are recorded live by wrapping requests. The bundled programs (Wyze SDK, go2rtc,
MQTT) don't use requests, so they're listed from what's configured instead.
"""

import time
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

import requests
from wyzebridge.mqtt import MQTT_ENABLED, MQTT_HOST

SEEN: dict[str, dict] = {}
PURPOSES = {
    "wyze.com": "Wyze account, camera list, events and controls",
    "wyzecam.com": "Wyze account, camera list, events and controls",
    "amazonaws.com": "Wyze cloud: thumbnails and WebRTC",
    "api.github.com": "Release notes for the update alert",
    "ntfy.sh": "Phone alerts",
    "discord.com": "Chat alerts",
    "discordapp.com": "Chat alerts",
    "hooks.slack.com": "Chat alerts",
    "api.telegram.org": "Chat alerts",
    "supervisor": "Home Assistant",
}


def purpose(host: str) -> str:
    return next((label for domain, label in PURPOSES.items() if host == domain or host.endswith("." + domain)), "")


def is_local(host: str) -> bool:
    try:
        return not ip_address(host).is_global
    except ValueError:
        return host in ("localhost", "supervisor") or host.endswith((".local", ".lan", ".home.arpa"))


_send = requests.Session.send


def _recording_send(self, request, **kwargs):
    host = urlsplit(request.url).hostname or ""
    if host in ("127.0.0.1", "localhost", "::1"):  # the app talking to itself
        return _send(self, request, **kwargs)
    entry = SEEN.setdefault(host, {"host": host, "purpose": purpose(host), "local": is_local(host), "count": 0, "error": ""})
    entry["count"] += 1
    entry["last"] = time.time()
    try:
        resp = _send(self, request, **kwargs)
    except requests.RequestException as ex:
        entry["error"] = type(ex).__name__
        raise
    entry["error"] = ""
    return resp


requests.Session.send = _recording_send


def cloud_down() -> bool:
    """True when the last call to Wyze failed to connect."""
    wyze = [e for e in SEEN.values() if e["host"].endswith(("wyze.com", "wyzecam.com"))]
    return bool(wyze) and max(wyze, key=lambda e: e["last"])["error"] in ("ConnectionError", "ConnectTimeout", "ProxyError", "SSLError")


def other_connections(cams: dict) -> list[dict]:
    """Connections made outside requests, from each camera's source and the MQTT setting."""
    rows = []
    sources = {cam.get("source") for cam in cams.values()}
    if "tutk" in sources:
        rows.append({"host": "Wyze/TUTK P2P servers", "purpose": "Wyze SDK finds cameras and relays them when they're not on your network", "local": False})
    if "lan" in sources:
        ips = sorted({cam["ip"] for cam in cams.values() if cam.get("source") == "lan" and cam.get("ip")})
        rows.append({"host": ", ".join(ips) or "your cameras", "purpose": "go2rtc talks to cameras directly on your network", "local": True})
    if "webrtc" in sources:
        rows.append({"host": "Amazon Kinesis (Wyze)", "purpose": "Wyze cloud WebRTC for cameras the other ways can't reach", "local": False})
    if MQTT_ENABLED:
        rows.append({"host": MQTT_HOST, "purpose": "MQTT broker", "local": is_local(MQTT_HOST)})
    return rows


def lan_keys_saved(path: str) -> float:
    """When the camera keys for offline use were last saved, or 0."""
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0
