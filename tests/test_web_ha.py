# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
import json
from types import SimpleNamespace
from unittest import mock

import yaml


def test_ha_dashboard_yaml(monkeypatch):
    import frontend
    from wyzebridge import web_ui

    cams = {
        "front": {"nickname": "Front Door", "enabled": True, "hls_url": "http://h:8888/front/", "webrtc_url": None},
        "back": {"nickname": "Back", "enabled": False, "hls_url": "http://h:8888/back/"},
    }
    monkeypatch.setattr(web_ui.auth, "verify_password_callback", lambda user, password: True)
    with mock.patch.object(frontend, "WyzeBridge"), mock.patch.object(web_ui, "all_cams", return_value={"cameras": cams}):
        body = frontend.create_app().test_client().get("/ha-dashboard.yaml").get_data(as_text=True)
    cards = yaml.safe_load(body)["views"][0]["cards"]
    assert cards == [{"type": "iframe", "title": "Front Door", "url": "http://h:8888/front/", "aspect_ratio": "16:9"}]


def test_bridge_sensors(monkeypatch):
    from wyzebridge import mqtt

    monkeypatch.setattr(mqtt, "MQTT_DISCOVERY", "homeassistant")
    monkeypatch.setattr(mqtt, "BRIDGE_DISCOVERY_SENT", False)
    client = mock.Mock()
    streams = {u: SimpleNamespace(status=lambda s=s: s) for u, s in [("a", "connected"), ("b", "offline"), ("c", "stopped")]}
    mqtt.bridge_status(client, streams)
    mqtt.bridge_status(client, streams)
    published = [c.args for c in client.publish.call_args_list]
    configs = [p for p in published if p[0].endswith("/config")]
    assert len(configs) == 3  # discovery only once
    assert json.loads(configs[0][1])["device"]["name"] == "Wyze Tunnel"
    assert ("wyzebridge/bridge/cameras_streaming", 1) in published
    assert ("wyzebridge/bridge/cameras_offline", 1) in published
