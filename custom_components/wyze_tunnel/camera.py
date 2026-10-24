from __future__ import annotations

import voluptuous as vol
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WyzeTunnelEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    async_add_entities(WyzeTunnelCamera(coordinator, cam_id) for cam_id in coordinator.data)
    entity_platform.async_get_current_platform().async_register_entity_service(
        "send_command", {vol.Required("command"): str, vol.Optional("value"): str}, "async_send_command"
    )


class WyzeTunnelCamera(WyzeTunnelEntity, Camera):
    _attr_name = None  # the device's name
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator, cam_id: str) -> None:
        WyzeTunnelEntity.__init__(self, coordinator, cam_id, "camera")
        Camera.__init__(self)

    @property
    def is_streaming(self) -> bool:
        return bool(self.cam.get("streaming"))

    @property
    def motion_detection_enabled(self) -> bool:
        return True

    @property
    def extra_state_attributes(self) -> dict:
        return {"wyze_tunnel_id": self.cam_id, "last_motion": self.cam.get("last_motion")}

    async def stream_source(self) -> str | None:
        return (self.cam.get("streams") or {}).get("rtsp")

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        return await self.coordinator.api.snapshot(self.cam_id)

    async def async_send_command(self, command: str, value: str | None = None) -> None:
        if self.coordinator.scope != "control":
            raise HomeAssistantError("This Wyze Tunnel key can only view. Make a new setup code to control cameras.")
        await self.coordinator.api.command(self.cam_id, command, value)
