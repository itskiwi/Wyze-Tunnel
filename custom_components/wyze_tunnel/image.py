"""The picture from each camera's newest event, for notifications and dashboards."""
from __future__ import annotations

from datetime import datetime, timezone

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WyzeTunnelEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    async_add_entities(LastEventImage(hass, coordinator, cam_id) for cam_id in coordinator.data)


class LastEventImage(WyzeTunnelEntity, ImageEntity):
    _attr_translation_key = "last_event"
    _attr_content_type = "image/jpeg"

    def __init__(self, hass: HomeAssistant, coordinator, cam_id: str) -> None:
        WyzeTunnelEntity.__init__(self, coordinator, cam_id, "last_event_image")
        ImageEntity.__init__(self, hass)
        self._update_time()

    def _update_time(self) -> None:
        ts = (self.cam.get("last_event") or {}).get("ts")
        when = datetime.fromtimestamp(ts, timezone.utc) if ts else None
        if when != self._attr_image_last_updated:
            self._attr_image_last_updated = when
            self._cached_image = None  # fetch the new picture next time it's asked for

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_time()
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        return await self.coordinator.api.last_event_image(self.cam_id)
