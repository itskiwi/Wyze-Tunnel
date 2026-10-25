"""Pan and tilt, for cameras that move, when the setup code's key can control cameras."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WyzeTunnelEntity

MOVES = {"left": "mdi:arrow-left-bold", "right": "mdi:arrow-right-bold", "up": "mdi:arrow-up-bold", "down": "mdi:arrow-down-bold"}


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    if coordinator.scope != "control":
        return
    async_add_entities(
        MoveButton(coordinator, cam_id, move)
        for cam_id, cam in coordinator.data.items()
        if (cam.get("features") or {}).get("pan_cam")
        for move in MOVES
    )


class MoveButton(WyzeTunnelEntity, ButtonEntity):
    def __init__(self, coordinator, cam_id: str, move: str) -> None:
        super().__init__(coordinator, cam_id, f"move_{move}")
        self.move = move
        self._attr_translation_key = f"move_{move}"
        self._attr_icon = MOVES[move]

    async def async_press(self) -> None:
        await self.coordinator.api.command(self.cam_id, "rotary_action", self.move)
