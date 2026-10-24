"""Controls, only when the setup code's key can control cameras."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WyzeTunnelEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    if coordinator.scope != "control":
        return
    entities = []
    for cam_id, cam in coordinator.data.items():
        entities.append(StreamSwitch(coordinator, cam_id))
        for light in ("spotlight", "floodlight"):
            if (cam.get("features") or {}).get(light):
                entities.append(LightSwitch(coordinator, cam_id, light))
    async_add_entities(entities)


class StreamSwitch(WyzeTunnelEntity, SwitchEntity):
    """Whether Wyze Tunnel keeps this camera's stream available."""

    _attr_name = "Stream"
    _attr_icon = "mdi:video"

    def __init__(self, coordinator, cam_id: str) -> None:
        super().__init__(coordinator, cam_id, "stream")

    @property
    def is_on(self) -> bool:
        return bool(self.cam.get("enabled"))

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.api.command(self.cam_id, "state", "enable")
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.api.command(self.cam_id, "state", "disable")
        await self.coordinator.async_request_refresh()


class LightSwitch(WyzeTunnelEntity, SwitchEntity):
    """The camera doesn't report its light, so this remembers the last thing it was told."""

    _attr_assumed_state = True
    _attr_is_on = False
    _attr_icon = "mdi:spotlight-beam"

    def __init__(self, coordinator, cam_id: str, light: str) -> None:
        super().__init__(coordinator, cam_id, light)
        self.light = light
        self._attr_name = light.title()

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.api.command(self.cam_id, self.light, "on")
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.api.command(self.cam_id, self.light, "off")
        self._attr_is_on = False
        self.async_write_ha_state()
