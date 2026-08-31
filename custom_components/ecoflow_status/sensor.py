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
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEVICE_PROFILE_BATTERY,
    DEVICE_PROFILE_PANEL,
    DOMAIN,
    MANUFACTURER,
    PANEL_PRODUCT_HINTS,
    POWER_THRESHOLD_W,
    STATE_CHARGING,
    STATE_DISCHARGING,
    STATE_STANDBY,
)
from .coordinator import EcoFlowStatusCoordinator

_LOGGER = logging.getLogger(__name__)


# A few key families in the EcoFlow quota response.
# Keys vary slightly across product lines, so we try a list in order.
KEY_SOC = (
    "cmsBattSoc",                    # Stream CA Pro / Ultra X (verified by user)
    "bmsBmsStatus.soc",
    "bmsBmsStatus.f32ShowSoc",
    "bms_emsStatus.f32ShowSoc",
)
KEY_SOH = (
    "cmsBattSoh",                    # Stream (best guess, needs verify)
    "bmsBmsStatus.soh",
    "bms_emsStatus.soh",
)
# Battery power: positive while charging, negative while discharging.
# Stream key is `powGetSysLoadFromBp` per toli's STREAM_GET_SYS_LOAD_FROM_BP.
# The sign convention may need flip if EcoFlow uses the opposite.
KEY_BATTERY_POWER = (
    "powGetSysLoadFromBp",
    "powGetBp",
    "powGetBatteryPower",
    "bmsBmsStatus.chargePower",       # Delta fallback (W)
    "pd.wattsInSum",
)
KEY_CHARGE_W = KEY_BATTERY_POWER
KEY_DISCHARGE_W = KEY_BATTERY_POWER
# 0=idle, 1=discharging, 2=charging (newer devices like Stream Ultra / CA Pro)
KEY_STATE_CODE_NEW = (
    "bms_emsStatus.sysChgDsgState",
    "cmsBattChgDsgState",            # Stream best guess
)
# 1=discharging, 2=charging (no idle=0 convention, missing == idle)
KEY_STATE_CODE_OLD = ("pd.chgDsgState",)
KEY_CYCLES = (
    "cmsBmsCycles",                   # Stream best guess
    "cmsBattCycles",
    "bmsBmsStatus.cycles",
    "bms_emsStatus.cycles",
)
# Charging remaining minutes (positive while charging, may be 0/undefined when not)
KEY_REMAIN_CHG_MIN = (
    "cmsBmsChgRemainTime",
    "cmsBattChgRemainTime",
    "bms_emsStatus.chgRemainTime",
    "bmsBmsStatus.remainTime",        # signed: positive=charging, negative=discharging
)
# Discharging remaining minutes (positive while discharging, may be 0/undefined when not)
KEY_REMAIN_DSG_MIN = (
    "cmsBmsDsgRemainTime",
    "cmsBattDsgRemainTime",
    "bms_emsStatus.dsgRemainTime",
)
# Stream energy flow keys (used to derive battery power when KEY_BATTERY_POWER is absent)
KEY_PV_SUM_W = ("powGetPvSum",)
KEY_SYS_LOAD_W = ("powGetSysLoad",)
KEY_SYS_GRID_W = ("powGetSysGrid",)

# Panel / inverter keys (Stream AC Pro, PowerStream, Smart Home Panel)
KEY_PANEL_GRID_W = ("powGetSysGrid", "gridConnectionPower")
KEY_PANEL_SOLAR_W = ("powGetPvSum",)
KEY_PANEL_LOAD_W = ("powGetSysLoad",)
KEY_PANEL_BATTERY_W = ("powGetBpCms", "bmsBmsStatus.chargePower")
KEY_PANEL_FEED_IN_MODE = ("feedGridMode",)
KEY_PANEL_BACKUP_SOC = ("backupReverseSoc",)
KEY_PANEL_ENERGY_STRATEGY = (
    "energyStrategyOperateMode.operateSelfPoweredOpen",
    "energyStrategyOperateMode.operateIntelligentScheduleModeOpen",
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


def _read_state_code(quota: dict[str, Any]) -> int | None:
    """Read the device-reported charge state code.

    Returns 0=idle, 1=discharging, 2=charging, or None when no key is present.
    Prefers `bms_emsStatus.sysChgDsgState` (newer, 0=idle/1=dis/2=chg) and
    falls back to `pd.chgDsgState` (older, 1=dis/2=chg, 0/missing = idle).
    """
    for key in KEY_STATE_CODE_NEW:
        val = quota.get(key)
        if val is not None:
            try:
                code = int(val)
                if code in (0, 1, 2):
                    return code
            except (TypeError, ValueError):
                pass
    for key in KEY_STATE_CODE_OLD:
        val = quota.get(key)
        if val is not None:
            try:
                code = int(val)
                if code in (1, 2):
                    return code
                if code == 0:
                    return 0
            except (TypeError, ValueError):
                pass
    return None


def _state_from_code(code: int) -> str:
    """Map an EcoFlow state code to our coarse state."""
    if code == 2:
        return STATE_CHARGING
    if code == 1:
        return STATE_DISCHARGING
    return STATE_STANDBY


def _derive_remaining_min(quota: dict[str, Any], keys: tuple[str, ...], signed: bool = False) -> float | None:
    """Read a remaining-time value (minutes) from the quota, robust to None / bad data.

    When `signed` is True, the value is treated as signed (positive=charging,
    negative=discharging) and we return its absolute value. Otherwise we expect
    a non-negative count of minutes.
    """
    for key in keys:
        if key not in quota:
            continue
        val = quota[key]
        if val is None:
            continue
        try:
            minutes = float(val)
        except (TypeError, ValueError):
            continue
        if signed:
            minutes = abs(minutes)
        if minutes < 0:
            continue
        return minutes
    return None


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
    SensorEntityDescription(
        key="cycles",
        translation_key="cycles",
        name="Cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:battery-sync",
    ),
    SensorEntityDescription(
        key="remaining_charge_time",
        translation_key="remaining_charge_time",
        name="Charge Remaining Time",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:battery-charging",
    ),
    SensorEntityDescription(
        key="remaining_discharge_time",
        translation_key="remaining_discharge_time",
        name="Discharge Remaining Time",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:battery-discharging",
    ),
)


# ---------------------------------------------------------------- panel sensors
# For smart home panels / inverters (Stream AC Pro, PowerStream, etc.) the
# battery-oriented sensors above don't make sense. These are the panel-specific
# sensors: grid in/out, solar, system load, panel battery power, feed-in mode,
# energy strategy, backup reserve.
PANEL_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="grid_power",
        translation_key="grid_power",
        name="Grid Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    SensorEntityDescription(
        key="solar_power",
        translation_key="solar_power",
        name="Solar Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    SensorEntityDescription(
        key="system_load",
        translation_key="system_load",
        name="System Load",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    SensorEntityDescription(
        key="panel_battery_power",
        translation_key="panel_battery_power",
        name="Battery Power (Panel)",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    SensorEntityDescription(
        key="feed_in_mode",
        translation_key="feed_in_mode",
        name="Feed-in Mode",
        icon="mdi:transmission-tower-export",
    ),
    SensorEntityDescription(
        key="backup_reserve_soc",
        translation_key="backup_reserve_soc",
        name="Backup Reserve",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
    ),
    SensorEntityDescription(
        key="energy_strategy",
        translation_key="energy_strategy",
        name="Energy Strategy",
        icon="mdi:lightning-bolt",
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


def _detect_device_profile(quota: dict[str, Any] | None) -> str:
    """Return DEVICE_PROFILE_BATTERY or DEVICE_PROFILE_PANEL.

    Looks at productName / productDetail from the quota. If the name contains
    a panel/inverter hint (e.g. "Stream AC Pro", "PowerStream"), treat as panel.
    Default to battery.
    """
    if not quota or not isinstance(quota, dict):
        return DEVICE_PROFILE_BATTERY
    name_candidates: list[str] = []
    for k in ("productName", "productDetail", "productType", "deviceName"):
        v = quota.get(k)
        if isinstance(v, str) and v:
            name_candidates.append(v.lower())
    for hint in PANEL_PRODUCT_HINTS:
        for name in name_candidates:
            if hint in name:
                return DEVICE_PROFILE_PANEL
    return DEVICE_PROFILE_BATTERY


# --------------------------------------------------------------------- platform


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from a config entry.

    The set of sensors per device depends on the product type detected from
    the first quota response:
    - battery devices (Ultra X, Delta, River): 8 battery sensors
    - panel devices (Stream AC Pro, PowerStream): 7 panel sensors
    """
    coordinator: EcoFlowStatusCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[EcoFlowSensorEntity] = []
    for sn in coordinator.selected_sns:
        quota = coordinator.data.get(sn) if coordinator.data else None
        profile = _detect_device_profile(quota)
        device_info = _build_device_info(coordinator, sn)
        if profile == DEVICE_PROFILE_PANEL:
            for desc in PANEL_SENSORS:
                entities.append(EcoFlowSensorEntity(coordinator, sn, desc, device_info))
        else:
            for desc in SENSORS_PER_DEVICE:
                entities.append(EcoFlowSensorEntity(coordinator, sn, desc, device_info))
    async_add_entities(entities)


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
            # Prefer the device-reported code (more reliable on idle / low-power).
            code = _read_state_code(quota)
            if code is not None:
                return _state_from_code(code)
            # Fall back to battery power (signed): +charging, -discharging.
            bp = _to_float(_read(quota, KEY_BATTERY_POWER))
            if bp is not None:
                if bp > POWER_THRESHOLD_W: return STATE_CHARGING
                if bp < -POWER_THRESHOLD_W: return STATE_DISCHARGING
                return STATE_STANDBY
            # Last resort: derive from PV/Load/Grid energy balance (Stream).
            pv = _to_float(_read(quota, KEY_PV_SUM_W)) or 0.0
            load = _to_float(_read(quota, KEY_SYS_LOAD_W)) or 0.0
            grid = _to_float(_read(quota, KEY_SYS_GRID_W)) or 0.0
            # Net battery flow: PV + Grid - Load. If > 0, battery absorbing (charging).
            net_battery = pv - load + grid
            if net_battery > POWER_THRESHOLD_W: return STATE_CHARGING
            if net_battery < -POWER_THRESHOLD_W: return STATE_DISCHARGING
            return STATE_STANDBY
        if key == "charge_power":
            bp = _to_float(_read(quota, KEY_BATTERY_POWER))
            if bp is None:
                # Derive from energy balance
                pv = _to_float(_read(quota, KEY_PV_SUM_W)) or 0.0
                load = _to_float(_read(quota, KEY_SYS_LOAD_W)) or 0.0
                grid = _to_float(_read(quota, KEY_SYS_GRID_W)) or 0.0
                net_battery = pv - load + grid
                return max(net_battery, 0.0) if net_battery > POWER_THRESHOLD_W else 0.0
            # Signed: positive = charging
            return max(bp, 0.0) if bp > POWER_THRESHOLD_W else 0.0
        if key == "discharge_power":
            bp = _to_float(_read(quota, KEY_BATTERY_POWER))
            if bp is None:
                pv = _to_float(_read(quota, KEY_PV_SUM_W)) or 0.0
                load = _to_float(_read(quota, KEY_SYS_LOAD_W)) or 0.0
                grid = _to_float(_read(quota, KEY_SYS_GRID_W)) or 0.0
                net_battery = pv - load + grid
                return max(-net_battery, 0.0) if net_battery < -POWER_THRESHOLD_W else 0.0
            # Signed: negative = discharging
            return max(-bp, 0.0) if bp < -POWER_THRESHOLD_W else 0.0
        if key == "soc":
            return _to_float(_read(quota, KEY_SOC))
        if key == "soh":
            return _to_float(_read(quota, KEY_SOH))
        if key == "cycles":
            return _to_float(_read(quota, KEY_CYCLES))
        if key == "remaining_charge_time":
            return _derive_remaining_min(quota, KEY_REMAIN_CHG_MIN, signed=True)
        if key == "remaining_discharge_time":
            return _derive_remaining_min(quota, KEY_REMAIN_DSG_MIN, signed=True)
        # Panel sensors
        if key == "grid_power":
            return _to_float(_read(quota, KEY_PANEL_GRID_W))
        if key == "solar_power":
            return _to_float(_read(quota, KEY_PANEL_SOLAR_W))
        if key == "system_load":
            return _to_float(_read(quota, KEY_PANEL_LOAD_W))
        if key == "panel_battery_power":
            return _to_float(_read(quota, KEY_PANEL_BATTERY_W))
        if key == "feed_in_mode":
            mode = _to_float(_read(quota, KEY_PANEL_FEED_IN_MODE))
            return int(mode) if mode is not None else None
        if key == "backup_reserve_soc":
            return _to_float(_read(quota, KEY_PANEL_BACKUP_SOC))
        if key == "energy_strategy":
            sp = _read(quota, "energyStrategyOperateMode.operateSelfPoweredOpen")
            sch = _read(quota, "energyStrategyOperateMode.operateIntelligentScheduleModeOpen")
            if sch is True or sch == "1" or sch == 1:
                return "Scheduled"
            if sp is True or sp == "1" or sp == 1:
                return "Self-powered"
            return "Standard"
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the full raw quota for debugging key mismatches.

        The first 30 keys are enough to see all relevant quota entries for any
        device family. Remove once KEY_* are confirmed for the user's devices.
        """
        quota = self.coordinator.data.get(self._sn) if self.coordinator.data else None
        if not quota or not isinstance(quota, dict):
            return None
        items = list(quota.items())[:30]
        return {"raw_quota_sample": dict(items)}
