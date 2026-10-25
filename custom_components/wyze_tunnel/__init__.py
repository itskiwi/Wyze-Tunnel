"""Wyze Tunnel: cameras, motion and controls from a Wyze Tunnel bridge, over its /api/v1 API."""
from __future__ import annotations

import base64
import json
import logging
from datetime import timedelta

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator, UpdateFailed

DOMAIN = "wyze_tunnel"
PLATFORMS = [
    Platform.CAMERA, Platform.BINARY_SENSOR, Platform.SWITCH, Platform.EVENT, Platform.SENSOR, Platform.BUTTON, Platform.SELECT
]
# Wyze Tunnel keeps a camera's motion flag up for 20 seconds, so polling every 5 can't miss one.
SCAN_INTERVAL = timedelta(seconds=5)
_LOGGER = logging.getLogger(__name__)


class AuthError(Exception):
    """The token was removed or is wrong."""


def parse_setup_code(code: str) -> tuple[str, str]:
    """The Apps page's setup code is "wt1." + base64url({"url", "token"}). Raises ValueError."""
    code = code.strip()
    if not code.startswith("wt1."):
        raise ValueError("not a setup code")
    raw = code[4:] + "=" * (-len(code[4:]) % 4)
    data = json.loads(base64.urlsafe_b64decode(raw))
    url, token = str(data["url"]).rstrip("/"), str(data["token"])
    if not url.startswith(("http://", "https://")) or not token.startswith("wt_"):
        raise ValueError("bad setup code")
    return url, token


class WyzeTunnelApi:
    def __init__(self, session: aiohttp.ClientSession, url: str, token: str) -> None:
        self.session, self.url = session, url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}

    async def request(self, method: str, path: str, **kwargs):
        timeout = aiohttp.ClientTimeout(total=kwargs.pop("timeout", 15))
        async with self.session.request(
            method, f"{self.url}/api/v1/{path}", headers=self.headers, timeout=timeout, **kwargs
        ) as resp:
            if resp.status in (401, 403) and method == "GET":
                raise AuthError
            if (resp.content_type or "").startswith("image/"):
                return await resp.read()
            body = await resp.json(content_type=None)
            if resp.status >= 400:
                raise UpdateFailed(body.get("error") if isinstance(body, dict) else resp.reason)
            return body

    async def info(self) -> dict:
        return await self.request("GET", "")

    async def cameras(self) -> list[dict]:
        return await self.request("GET", "cameras")

    async def snapshot(self, cam: str) -> bytes | None:
        try:
            body = await self.request("GET", f"cameras/{cam}/snapshot", timeout=25)
        except (UpdateFailed, aiohttp.ClientError, TimeoutError):
            return None
        return body if isinstance(body, bytes) else None

    async def command(self, cam: str, cmd: str, value=None) -> dict:
        return await self.request("POST", f"cameras/{cam}/commands/{cmd}", json={"value": value}, timeout=30)

    async def mode(self) -> str:
        return (await self.request("GET", "mode"))["mode"]

    async def set_mode(self, mode: str) -> dict:
        return await self.request("POST", "mode", json={"mode": mode})


class WyzeTunnelCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: WyzeTunnelApi, scope: str) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=SCAN_INTERVAL)
        self.api, self.scope = api, scope
        self.mode: str | None = None  # Home, Away or schedule; only read with a control key, which can change it

    async def _async_update_data(self) -> dict[str, dict]:
        try:
            if self.scope == "control":
                self.mode = await self.api.mode()
            return {cam["id"]: cam for cam in await self.api.cameras()}
        except AuthError as ex:
            raise ConfigEntryAuthFailed("The token was removed in Wyze Tunnel") from ex
        except (aiohttp.ClientError, TimeoutError) as ex:
            raise UpdateFailed(f"Can't reach Wyze Tunnel at {self.api.url}") from ex


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    url, token = parse_setup_code(entry.data["setup_code"])
    api = WyzeTunnelApi(async_get_clientsession(hass), url, token)
    try:
        info = await api.info()
    except AuthError as ex:
        raise ConfigEntryAuthFailed("The token was removed in Wyze Tunnel") from ex
    coordinator = WyzeTunnelCoordinator(hass, entry, api, info.get("scope", "view"))
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


class WyzeTunnelEntity(CoordinatorEntity[WyzeTunnelCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: WyzeTunnelCoordinator, cam_id: str, key: str) -> None:
        super().__init__(coordinator)
        self.cam_id = cam_id
        base = coordinator.config_entry.unique_id
        self._attr_unique_id = f"{base}_{cam_id}_{key}"
        cam = coordinator.data[cam_id]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{base}_{cam_id}")},
            name=cam["name"],
            manufacturer="Wyze",
            model=cam.get("model"),
            configuration_url=coordinator.api.url,
        )

    @property
    def cam(self) -> dict:
        return self.coordinator.data.get(self.cam_id, {})

    @property
    def available(self) -> bool:
        return super().available and self.cam_id in self.coordinator.data


def bridge_device(coordinator: WyzeTunnelCoordinator) -> DeviceInfo:
    """Wyze Tunnel itself, for things that aren't about one camera."""
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.config_entry.unique_id)},
        name="Wyze Tunnel",
        manufacturer="Wyze Tunnel",
        configuration_url=coordinator.api.url,
    )
