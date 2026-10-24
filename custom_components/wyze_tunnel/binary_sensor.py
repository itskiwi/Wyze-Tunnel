from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WyzeTunnelEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        sensor(coordinator, cam_id) for cam_id in coordinator.data for sensor in (MotionSensor, OnlineSensor)
    )


class MotionSensor(WyzeTunnelEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(self, coordinator, cam_id: str) -> None:
        super().__init__(coordinator, cam_id, "motion")

    @property
    def is_on(self) -> bool:
        return bool(self.cam.get("motion"))


class OnlineSensor(WyzeTunnelEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, cam_id: str) -> None:
        super().__init__(coordinator, cam_id, "online")

    @property
    def is_on(self) -> bool:
        return bool(self.cam.get("online"))
