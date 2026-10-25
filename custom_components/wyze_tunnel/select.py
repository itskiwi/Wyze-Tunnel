"""Home, Away or schedule for alerts, so presence automations can switch it."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import bridge_device


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    if coordinator.scope == "control" and coordinator.mode:
        async_add_entities([ModeSelect(coordinator)])


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
