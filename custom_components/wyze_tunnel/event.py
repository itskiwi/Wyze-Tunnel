"""Motion, doorbell presses and what the detector saw, as events to start automations from."""
from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WyzeTunnelEntity

# The detector's labels, grouped the way people write automations.
KINDS = {
    "car": "vehicle", "truck": "vehicle", "bus": "vehicle", "motorcycle": "vehicle", "bicycle": "vehicle",
    "dog": "animal", "cat": "animal", "bird": "animal",
}
EVENT_TYPES = ["motion", "doorbell", "person", "vehicle", "animal", "package"]


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    async_add_entities(CameraEvent(coordinator, cam_id) for cam_id in coordinator.data)


class CameraEvent(WyzeTunnelEntity, EventEntity):
    _attr_translation_key = "activity"
    _attr_event_types = EVENT_TYPES

    def __init__(self, coordinator, cam_id: str) -> None:
        super().__init__(coordinator, cam_id, "event")
        if (self.cam.get("features") or {}).get("doorbell"):
            self._attr_device_class = EventDeviceClass.DOORBELL
        # Only events that happen after Home Assistant starts, not the one already in the bridge's log.
        self._seen = (self.cam.get("last_event") or {}).get("ts")

    @callback
    def _handle_coordinator_update(self) -> None:
        last = self.cam.get("last_event") or {}
        if last.get("ts") and last["ts"] != self._seen:
            self._seen = last["ts"]
            label = last.get("type") or "motion"
            kind = KINDS.get(label, label)
            self._trigger_event(kind if kind in EVENT_TYPES else "motion", {"label": label, "ts": last["ts"]})
        super()._handle_coordinator_update()
