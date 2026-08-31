"""Config flow for the EcoFlow Status integration."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EcoFlowAPIError, EcoFlowAuthError, EcoFlowClient
from .const import (
    CONF_ACCESS_KEY,
    CONF_DEVICES,
    CONF_REGION,
    CONF_SCAN_INTERVAL,
    CONF_SECRET_KEY,
    DEFAULT_REGION,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    REGIONS,
)

_LOGGER = logging.getLogger(__name__)


STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_REGION, default=DEFAULT_REGION): vol.In(list(REGIONS)),
        vol.Required(CONF_ACCESS_KEY): cv.string,
        vol.Required(CONF_SECRET_KEY): cv.string,
    }
)


async def _try_connect(
    session: aiohttp.ClientSession, region: str, access_key: str, secret_key: str
) -> list[dict[str, Any]]:
    """Probe the EcoFlow API and return the list of devices on success."""
    client = EcoFlowClient(session, access_key, secret_key, region=region)
    return await client.list_devices()


class EcoFlowStatusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for EcoFlow Status."""

    VERSION = 1

    def __init__(self) -> None:
        self._region: str = DEFAULT_REGION
        self._access_key: str = ""
        self._secret_key: str = ""
        self._devices: list[dict[str, Any]] = []

    # ----------------------------------------------------------------- step user

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            try:
                devices = await _try_connect(
                    session,
                    user_input[CONF_REGION],
                    user_input[CONF_ACCESS_KEY],
                    user_input[CONF_SECRET_KEY],
                )
            except EcoFlowAuthError:
                errors["base"] = "invalid_auth"
            except EcoFlowAPIError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during EcoFlow auth")
                errors["base"] = "unknown"
            else:
                if not devices:
                    errors["base"] = "no_devices"
                else:
                    self._region = user_input[CONF_REGION]
                    self._access_key = user_input[CONF_ACCESS_KEY]
                    self._secret_key = user_input[CONF_SECRET_KEY]
                    self._devices = devices
                    return await self.async_step_devices()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    # ------------------------------------------------------------ step devices

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick which devices to expose as entities."""
        device_options = {
            d["sn"]: (
                d.get("deviceName")
                or d.get("productName")
                or d["sn"]
            )
            for d in self._devices
        }
        if user_input is not None:
            selected = list(user_input.get(CONF_DEVICES, []))
            if not selected:
                return self.async_show_form(
                    step_id="devices",
                    data_schema=vol.Schema(
                        {vol.Required(CONF_DEVICES): cv.multi_select(device_options)}
                    ),
                    errors={"base": "no_device_selected"},
                )
            # De-duplicate by SN (a user could conceivably have duplicates).
            unique_id = f"{self._region}:{','.join(sorted(selected))}"
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="EcoFlow Status",
                data={
                    CONF_REGION: self._region,
                    CONF_ACCESS_KEY: self._access_key,
                    CONF_SECRET_KEY: self._secret_key,
                    CONF_DEVICES: selected,
                },
                options={CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL},
            )

        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(
                {vol.Required(CONF_DEVICES): cv.multi_select(device_options)}
            ),
        )

    # --------------------------------------------------------------- options

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlow:
        return EcoFlowStatusOptionsFlow(config_entry)


class EcoFlowStatusOptionsFlow(OptionsFlow):
    """Options flow: change polling interval and re-pick devices."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self.config_entry = config_entry
        self._devices: list[dict[str, Any]] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SCAN_INTERVAL, default=current_interval
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
