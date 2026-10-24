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
    "motion": True, "last_motion": 1, "features": {"spotlight": True},
    "streams": {"rtsp": "rtsp://apps:k@bridge.local:8554/front"},
}


def mock_bridge(aioclient_mock, scope="control"):
    aioclient_mock.get(f"{URL}/api/v1/", json={"name": "Wyze Tunnel", "scope": scope})
    aioclient_mock.get(f"{URL}/api/v1/cameras", json=[CAM])
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


async def test_removed_token_asks_to_reconnect(hass: HomeAssistant, aioclient_mock):
    aioclient_mock.get(f"{URL}/api/v1/", status=401, json={"error": "Unauthorized"})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=URL, data={"setup_code": CODE})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())
