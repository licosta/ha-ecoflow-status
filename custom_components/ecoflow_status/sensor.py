"""Sensor platform for the EcoFlow Status integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    MANUFACTURER,
    POWER_THRESHOLD_W,
    STATE_CHARGING,
    STATE_DISCHARGING,
    STATE_STANDBY,
)
from .coordinator import EcoFlowStatusCoordinator

_LOGGER = logging.getLogger(__name__)


# A few key families in the EcoFlow quota response.
# Keys vary slightly across product lines, so we try a list in order.
KEY_SOC = ("bmsBmsStatus.soc", "bmsBmsStatus.f32ShowSoc", "bms_emsStatus.f32ShowSoc")
KEY_SOH = ("bmsBmsStatus.soh", "bms_emsStatus.soh")
KEY_CHARGE_W = (
    "bmsBmsStatus.chargePower",  # W (Stream series) or mW (some Delta) - we keep raw and the entity converts
    "pd.wattsInSum",
)
KEY_DISCHARGE_W = (
    "bmsBmsStatus.dischargePower",
    "inv.outputWatts",
    "pd.wattsOutSum",
)


def _read(quota: dict[str, Any], keys: tuple[str, ...]) -> Any | None:
    for k in keys:
        if k in quota:
            return quota[k]
    return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _derive_state(charge_w: float | None, discharge_w: float | None) -> str | None:
    """Map current power to a coarse state. None when no data."""
    if charge_w is None and discharge_w is None:
        return None
    c = charge_w or 0.0
    d = discharge_w or 0.0
    if c > POWER_THRESHOLD_W and d <= POWER_THRESHOLD_W:
        return STATE_CHARGING
    if d > POWER_THRESHOLD_W and c <= POWER_THRESHOLD_W:
        return STATE_DISCHARGING
    return STATE_STANDBY


# --------------------------------------------------------------------- descriptions


SENSORS_PER_DEVICE: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="state",
        translation_key="state",
        name="State",
        device_class=SensorDeviceClass.ENUM,
        options=[STATE_CHARGING, STATE_DISCHARGING, STATE_STANDBY],
    ),
    SensorEntityDescription(
        key="charge_power",
        translation_key="charge_power",
        name="Charge Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    SensorEntityDescription(
        key="discharge_power",
        translation_key="discharge_power",
        name="Discharge Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    SensorEntityDescription(
        key="soc",
        translation_key="soc",
        name="Battery",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
    ),
    SensorEntityDescription(
        key="soh",
        translation_key="soh",
        name="State of Health",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
    ),
)


# --------------------------------------------------------------------- platform


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from a config entry."""
    coordinator: EcoFlowStatusCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[EcoFlowSensorEntity] = []
    for sn in coordinator.selected_sns:
        device_info = _build_device_info(coordinator, sn)
        for desc in SENSORS_PER_DEVICE:
            entities.append(EcoFlowSensorEntity(coordinator, sn, desc, device_info))
    async_add_entities(entities)


def _build_device_info(
    coordinator: EcoFlowStatusCoordinator, sn: str
) -> DeviceInfo:
    """Build HA DeviceInfo for one EcoFlow device using cached metadata.

    Metadata (model, sw_version) is sourced from the first quota response
    that contains `productName` / `productDetail` / `sn` fields, if any.
    """
    quota = coordinator.data.get(sn) if coordinator.data else None
    name = None
    model = None
    sw_version = None
    if quota:
        # EcoFlow returns a flat dict; sometimes product info is mixed in.
        for k in (
            "productName",
            "productType",
            "productDetail",
            "deviceName",
        ):
            if k in quota and quota[k]:
                name = name or str(quota[k])
                break
        for k in ("productName", "productDetail"):
            if k in quota and quota[k]:
                model = str(quota[k])
                break
        for k in ("sn",):
            pass
    if not name:
        name = f"EcoFlow {sn[-6:]}"
    return DeviceInfo(
        identifiers={(DOMAIN, sn)},
        manufacturer=MANUFACTURER,
        model=model or "EcoFlow device",
        name=name,
        sw_version=sw_version,
        serial_number=sn,
    )


# --------------------------------------------------------------------- entity


class EcoFlowSensorEntity(CoordinatorEntity[EcoFlowStatusCoordinator], SensorEntity):
    """One sensor per (device, metric)."""

    _attr_has_entity_name = True
    entity_description: SensorEntityDescription

    def __init__(
        self,
        coordinator: EcoFlowStatusCoordinator,
        sn: str,
        description: SensorEntityDescription,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._sn = sn
        # Use last 6 chars of the SN so entity_ids stay short and unique.
        self._attr_unique_id = f"{sn}_{description.key}"
        self._attr_device_info = device_info
        # entity_id will be: sensor.ecoflow_status_<last6>_<key>

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        quota = self.coordinator.data.get(self._sn) if self.coordinator.data else None
        return quota is not None

    @property
    def native_value(self) -> Any:
        quota = self.coordinator.data.get(self._sn) if self.coordinator.data else None
        if not quota:
            return None
        key = self.entity_description.key
        if key == "state":
            charge = _to_float(_read(quota, KEY_CHARGE_W))
            discharge = _to_float(_read(quota, KEY_DISCHARGE_W))
            return _derive_state(charge, discharge)
        if key == "charge_power":
            return _to_float(_read(quota, KEY_CHARGE_W))
        if key == "discharge_power":
            return _to_float(_read(quota, KEY_DISCHARGE_W))
        if key == "soc":
            return _to_float(_read(quota, KEY_SOC))
        if key == "soh":
            return _to_float(_read(quota, KEY_SOH))
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
