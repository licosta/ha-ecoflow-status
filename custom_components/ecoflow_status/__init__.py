"""The EcoFlow Status integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EcoFlowClient
from .const import (
    CONF_ACCESS_KEY,
    CONF_DEVICES,
    CONF_REGION,
    CONF_SECRET_KEY,
    DOMAIN,
)
from .coordinator import EcoFlowStatusCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EcoFlow Status from a config entry."""
    session = async_get_clientsession(hass)
    client = EcoFlowClient(
        session,
        access_key=entry.data[CONF_ACCESS_KEY],
        secret_key=entry.data[CONF_SECRET_KEY],
        region=entry.data[CONF_REGION],
    )
    selected_sns: list[str] = list(entry.data.get(CONF_DEVICES, []))
    coordinator = EcoFlowStatusCoordinator(hass, client, entry, selected_sns)
    # Fetch the device list once to map SN -> productName. The quota response
    # doesn't include productName, so we need this to pick the right sensor
    # set per device (battery vs panel).
    await coordinator.async_refresh_device_models()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an EcoFlow Status config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change (e.g. scan_interval)."""
    await hass.config_entries.async_reload(entry.entry_id)
