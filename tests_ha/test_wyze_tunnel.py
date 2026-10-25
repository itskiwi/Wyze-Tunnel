import base64
import json

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wyze_tunnel import DOMAIN, parse_setup_code

URL = "http://bridge.local:5000"
TOKEN = "wt_abc"
CODE = "wt1." + base64.urlsafe_b64encode(json.dumps({"url": URL, "token": TOKEN}).encode()).decode().rstrip("=")
CAM = {
    "id": "front", "name": "Front Door", "model": "Wyze Cam v3", "online": True, "streaming": True, "enabled": True,
    "motion": True, "last_motion": 1, "features": {"spotlight": True, "doorbell": True, "pan_cam": True},
    "wifi": 72, "issues": ["weak_wifi"], "last_event": {"type": "motion", "ts": 1},
    "streams": {"rtsp": "rtsp://apps:k@bridge.local:8554/front"},
}


def mock_bridge(aioclient_mock, scope="control"):
    aioclient_mock.get(f"{URL}/api/v1/", json={"name": "Wyze Tunnel", "scope": scope})
    aioclient_mock.get(f"{URL}/api/v1/cameras", json=[CAM])
    aioclient_mock.get(f"{URL}/api/v1/mode", json={"mode": "schedule"})
    aioclient_mock.post(f"{URL}/api/v1/mode", json={"mode": "away"})
    aioclient_mock.post(f"{URL}/api/v1/cameras/front/commands/rotary_action", json={"status": "success"})
    aioclient_mock.get(f"{URL}/api/v1/cameras/front/last-event.jpg", content=b"jpeg", headers={"Content-Type": "image/jpeg"})
    aioclient_mock.get(f"{URL}/api/v1/cameras/front/recordings", json=[{"start": "2026-10-24T18:00:00Z", "duration": 600.0}])
    aioclient_mock.get(f"{URL}/api/v1/cameras/front/recordings/play", content=b"mp4data", headers={"Content-Type": "video/mp4"})
    aioclient_mock.post(f"{URL}/api/v1/cameras/front/commands/spotlight", json={"status": "success"})
    aioclient_mock.post(f"{URL}/api/v1/cameras/front/commands/night_vision", json={"status": "success"})


def test_parse_setup_code():
    assert parse_setup_code(f"  {CODE}\n") == (URL, TOKEN)


async def test_flow_one_paste(hass: HomeAssistant, aioclient_mock):
    mock_bridge(aioclient_mock)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    bad = await hass.config_entries.flow.async_configure(result["flow_id"], {"setup_code": "hello"})
    assert bad["errors"] == {"setup_code": "invalid_code"}
    done = await hass.config_entries.flow.async_configure(result["flow_id"], {"setup_code": CODE})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert aioclient_mock.mock_calls[-1][3]["Authorization"] == f"Bearer {TOKEN}"


async def test_flow_rejected_token(hass: HomeAssistant, aioclient_mock):
    aioclient_mock.get(f"{URL}/api/v1/", status=401, json={"error": "Unauthorized"})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    bad = await hass.config_entries.flow.async_configure(result["flow_id"], {"setup_code": CODE})
    assert bad["errors"] == {"setup_code": "invalid_auth"}


async def test_entities_and_commands(hass: HomeAssistant, aioclient_mock):
    mock_bridge(aioclient_mock)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=URL, data={"setup_code": CODE})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    assert hass.states.get("camera.front_door").state == "streaming"
    assert hass.states.get("binary_sensor.front_door_motion").state == "on"
    assert hass.states.get("binary_sensor.front_door_connectivity").state == "on"
    assert hass.states.get("switch.front_door_stream").state == "on"

    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.front_door_spotlight"}, blocking=True)
    assert hass.states.get("switch.front_door_spotlight").state == "on"
    await hass.services.async_call(
        DOMAIN, "send_command", {"entity_id": "camera.front_door", "command": "night_vision", "value": "on"}, blocking=True
    )
    posts = [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert [json.loads(json.dumps(c[2])) for c in posts] == [{"value": "on"}, {"value": "on"}]


async def test_view_only_has_no_switches(hass: HomeAssistant, aioclient_mock):
    mock_bridge(aioclient_mock, scope="view")
    entry = MockConfigEntry(domain=DOMAIN, unique_id=URL, data={"setup_code": CODE})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("camera.front_door") is not None
    assert hass.states.get("switch.front_door_stream") is None
    assert hass.states.get("button.front_door_pan_left") is None
    assert hass.states.get("select.wyze_tunnel_alert_mode") is None


async def test_removed_token_asks_to_reconnect(hass: HomeAssistant, aioclient_mock):
    aioclient_mock.get(f"{URL}/api/v1/", status=401, json={"error": "Unauthorized"})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=URL, data={"setup_code": CODE})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


async def setup(hass, aioclient_mock, scope="control"):
    mock_bridge(aioclient_mock, scope)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=URL, data={"setup_code": CODE})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_health_sensors_and_bridge_controls(hass: HomeAssistant, aioclient_mock):
    await setup(hass, aioclient_mock)
    assert hass.states.get("sensor.front_door_wi_fi_signal").state == "72"
    assert hass.states.get("sensor.front_door_last_motion").state.startswith("1970-01-01")
    problem = hass.states.get("binary_sensor.front_door_problem")
    assert problem.state == "on" and problem.attributes["issues"] == ["weak_wifi"]

    assert hass.states.get("select.wyze_tunnel_alert_mode").state == "schedule"
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.wyze_tunnel_alert_mode", "option": "away"}, blocking=True
    )
    await hass.services.async_call("button", "press", {"entity_id": "button.front_door_pan_left"}, blocking=True)
    posts = {str(c[1]): c[2] for c in aioclient_mock.mock_calls if c[0] == "POST"}
    assert posts[f"{URL}/api/v1/mode"] == {"mode": "away"}
    assert posts[f"{URL}/api/v1/cameras/front/commands/rotary_action"] == {"value": "left"}


async def test_new_detections_fire_events(hass: HomeAssistant, aioclient_mock):
    entry = await setup(hass, aioclient_mock)
    event = hass.states.get("event.front_door_activity")
    assert event.state == "unknown"  # the event already in the log when Home Assistant started isn't replayed
    assert event.attributes["device_class"] == "doorbell"

    coordinator = entry.runtime_data
    for label, kind in (("car", "vehicle"), ("doorbell", "doorbell"), ("kangaroo", "motion")):
        cam = coordinator.data["front"] | {"last_event": {"type": label, "ts": coordinator.data["front"]["last_event"]["ts"] + 1}}
        coordinator.async_set_updated_data({"front": cam})
        await hass.async_block_till_done()
        event = hass.states.get("event.front_door_activity")
        assert (event.attributes["event_type"], event.attributes["label"]) == (kind, label)


async def test_diagnostics_hide_secrets(hass: HomeAssistant, aioclient_mock):
    from custom_components.wyze_tunnel.diagnostics import async_get_config_entry_diagnostics

    entry = await setup(hass, aioclient_mock)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert TOKEN not in json.dumps(diag) and "apps:k" not in json.dumps(diag)
    assert diag["cameras"]["front"]["wifi"] == 72


async def test_last_event_picture_and_night_vision(hass: HomeAssistant, aioclient_mock, hass_client):
    await setup(hass, aioclient_mock)
    assert hass.states.get("switch.front_door_siren") is None  # doorbells have no siren
    client = await hass_client()
    resp = await client.get(hass.states.get("image.front_door_last_event").attributes["entity_picture"])
    assert resp.status == 200 and await resp.read() == b"jpeg"

    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.front_door_night_vision", "option": "auto"}, blocking=True
    )
    assert hass.states.get("select.front_door_night_vision").state == "auto"


async def test_recordings_in_media_browser(hass: HomeAssistant, aioclient_mock, hass_client, hass_client_no_auth):
    from homeassistant.components import media_source
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "media_source", {})
    entry = await setup(hass, aioclient_mock)
    root = await media_source.async_browse_media(hass, f"media-source://{DOMAIN}")
    assert [c.title for c in root.children] == ["Front Door"]
    cam = await media_source.async_browse_media(hass, root.children[0].media_content_id)
    assert len(cam.children) == 1 and cam.children[0].title.endswith("10 min")
    play = await media_source.async_resolve_media(hass, cam.children[0].media_content_id, None)
    assert play.mime_type == "video/mp4" and play.url.startswith(f"/api/wyze_tunnel/clip/{entry.entry_id}/front/")

    client = await hass_client()
    resp = await client.get(play.url)
    assert resp.status == 200 and await resp.read() == b"mp4data"
    sent = [c for c in aioclient_mock.mock_calls if c[1].path.endswith("/recordings/play")][-1]
    assert sent[3]["Authorization"] == f"Bearer {TOKEN}"
    unsigned = await (await hass_client_no_auth()).get(play.url.split("?")[0])
    assert unsigned.status == 401  # only the signed link from the Media browser plays without a login
