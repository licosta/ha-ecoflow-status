"""DataUpdateCoordinator for the EcoFlow Status integration."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EcoFlowAPIError, EcoFlowClient
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class EcoFlowStatusCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Polls the EcoFlow Open API and exposes a per-device quota dict.

    `self.data[sn]` is a flat dict of dotted quota keys -> raw values,
    e.g. {"bmsBmsStatus.chargePower": 85000, "bmsBmsStatus.soc": 40, ...}.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: EcoFlowClient,
        entry: ConfigEntry,
        selected_sns: list[str],
    ) -> None:
        scan_seconds = entry.options.get(
            "scan_interval", DEFAULT_SCAN_INTERVAL
        )
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=scan_seconds),
        )
        self.client = client
        self.entry = entry
        self._selected_sns = list(selected_sns)
        # sn -> productName (e.g. "Stream Ultra X", "Stream AC Pro"). Populated
        # by the integration setup via list_devices(); the sensor platform uses
        # this to pick battery vs panel sensors.
        self.device_models: dict[str, str] = {}

    @property
    def selected_sns(self) -> list[str]:
        return self._selected_sns

    async def async_refresh_device_models(self) -> None:
        """Fetch the device list once at startup and cache productName per SN.

        Done separately from the quota poll because the productName is only
        available in /device/list, not in the per-device quota response.
        """
        try:
            devices = await self.client.list_devices()
        except EcoFlowAPIError as err:
            _LOGGER.warning("Could not fetch device list for productName mapping: %s", err)
            return
        for d in devices:
            sn = d.get("sn")
            name = d.get("productName") or d.get("productType") or ""
            if sn:
                self.device_models[sn] = str(name)
        _LOGGER.debug("Device models: %s", self.device_models)

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Fetch all-quota for every selected device. Fail loud if any device fails."""
        if not self._selected_sns:
            return {}
        results: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        # Cache last-known data so we can fall back to it on per-device errors.
        # `self.data` is None on the first refresh, so guard explicitly.
        previous = self.data if self.data is not None else {}
        for sn in self._selected_sns:
            try:
                results[sn] = await self.client.get_all_quota(sn)
            except EcoFlowAPIError as err:
                # Keep last-known data; log the error. Don't blow up the whole poll.
                _LOGGER.warning("Failed to fetch quota for %s: %s", sn, err)
                errors.append(f"{sn}: {err}")
                if sn in previous:
                    results[sn] = previous[sn]
        if not results and errors:
            # No data at all -> surface the error so entities go unavailable.
            raise UpdateFailed("; ".join(errors))
        return results
