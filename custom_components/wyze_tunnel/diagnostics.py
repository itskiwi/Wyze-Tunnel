"""The "Download diagnostics" file, with stream passwords and the key left out."""
from __future__ import annotations

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

REDACT = {"setup_code", "stream_login", "streams"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry) -> dict:
    coordinator = entry.runtime_data
    return {
        "scope": coordinator.scope,
        "mode": coordinator.mode,
        "last_update_success": coordinator.last_update_success,
        "cameras": async_redact_data(coordinator.data, REDACT),
    }
