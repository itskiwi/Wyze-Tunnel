"""Set up with one paste: the setup code from Wyze Tunnel's Connect apps page."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import DOMAIN, AuthError, WyzeTunnelApi, parse_setup_code

SCHEMA = vol.Schema({vol.Required("setup_code"): str})


class WyzeTunnelConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _check(self, code: str) -> tuple[str, dict[str, str]]:
        """The bridge's address, and any error to show."""
        try:
            url, token = parse_setup_code(code)
        except (ValueError, KeyError, TypeError):
            return "", {"setup_code": "invalid_code"}
        try:
            await WyzeTunnelApi(async_get_clientsession(self.hass), url, token).info()
        except AuthError:
            return url, {"setup_code": "invalid_auth"}
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return url, {"base": "cannot_connect"}
        return url, {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url, errors = await self._check(user_input["setup_code"])
            if not errors:
                await self.async_set_unique_id(url)
                self._abort_if_unique_id_configured(updates={"setup_code": user_input["setup_code"].strip()})
                return self.async_create_entry(title="Wyze Tunnel", data={"setup_code": user_input["setup_code"].strip()})
        return self.async_show_form(step_id="user", data_schema=SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            _, errors = await self._check(user_input["setup_code"])
            if not errors:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(), data={"setup_code": user_input["setup_code"].strip()}
                )
        return self.async_show_form(step_id="reauth_confirm", data_schema=SCHEMA, errors=errors)
