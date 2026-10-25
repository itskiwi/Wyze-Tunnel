from __future__ import annotations

from datetime import datetime, timezone

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WyzeTunnelEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        sensor(coordinator, cam_id) for cam_id in coordinator.data for sensor in (WifiSensor, LastMotionSensor)
    )


class WifiSensor(WyzeTunnelEntity, SensorEntity):
    _attr_translation_key = "wifi"
    _attr_icon = "mdi:wifi"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, cam_id: str) -> None:
        super().__init__(coordinator, cam_id, "wifi")

    @property
    def native_value(self) -> int | None:
        return self.cam.get("wifi")


class LastMotionSensor(WyzeTunnelEntity, SensorEntity):
    _attr_translation_key = "last_motion"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, cam_id: str) -> None:
        super().__init__(coordinator, cam_id, "last_motion")

    @property
    def native_value(self) -> datetime | None:
        ts = self.cam.get("last_motion")
        return datetime.fromtimestamp(ts, timezone.utc) if ts else None
