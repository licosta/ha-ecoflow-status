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

    @property
    def selected_sns(self) -> list[str]:
        return self._selected_sns

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
