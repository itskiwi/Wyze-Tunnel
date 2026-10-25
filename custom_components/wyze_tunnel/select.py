"""Home, Away or schedule for alerts, so presence automations can switch it, and night vision per camera."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WyzeTunnelEntity, bridge_device


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    if coordinator.scope != "control":
        return
    entities = [NightVisionSelect(coordinator, cam_id) for cam_id in coordinator.data]
    if coordinator.mode:
        entities.append(ModeSelect(coordinator))
    async_add_entities(entities)


class ModeSelect(CoordinatorEntity, SelectEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "mode"
    _attr_icon = "mdi:home-account"
    _attr_options = ["schedule", "home", "away"]

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_mode"
        self._attr_device_info = bridge_device(coordinator)

    @property
    def current_option(self) -> str | None:
        return self.coordinator.mode

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.api.set_mode(option)
        await self.coordinator.async_request_refresh()


class NightVisionSelect(WyzeTunnelEntity, SelectEntity):
    """Asking every camera for its setting each poll is slow, so this shows the last thing it was told."""

    _attr_translation_key = "night_vision"
    _attr_icon = "mdi:weather-night"
    _attr_options = ["auto", "on", "off"]
    _attr_assumed_state = True
    _attr_current_option = None

    def __init__(self, coordinator, cam_id: str) -> None:
        super().__init__(coordinator, cam_id, "night_vision")

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.api.command(self.cam_id, "night_vision", option)
        self._attr_current_option = option
        self.async_write_ha_state()
