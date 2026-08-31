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
    DEVICE_PROFILE_HYBRID,
    DEVICE_PROFILE_PANEL,
    DOMAIN,
    HYBRID_PRODUCT_HINTS,
    HYBRID_SN_SUFFIXES,
    KNOWN_DEVICE_MODELS,
    MANUFACTURER,
    PANEL_PRODUCT_HINTS,
    PANEL_SN_SUFFIXES,
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
    "powGetBpCms",                    # Stream series (CA Pro, Ultra X) - signed: +chg / -dsg
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
# Per-MPPT solar inputs on the Stream Ultra X (4 strings). Tuple order is the
# fallback chain - if EcoFlow uses a different naming on a future firmware, the
# new key just needs to be added to the tuple.
KEY_PV1_W = (
    "powGetPv1",
    "powGetPv1InputW",
    "powGetPv1Power",
    "mppt1.inputPower",
    "powGetMppt1",
)
KEY_PV2_W = (
    "powGetPv2",
    "powGetPv2InputW",
    "powGetPv2Power",
    "mppt2.inputPower",
    "powGetMppt2",
)
KEY_PV3_W = (
    "powGetPv3",
    "powGetPv3InputW",
    "powGetPv3Power",
    "mppt3.inputPower",
    "powGetMppt3",
)
KEY_PV4_W = (
    "powGetPv4",
    "powGetPv4InputW",
    "powGetPv4Power",
    "mppt4.inputPower",
    "powGetMppt4",
)
KEY_SYS_LOAD_W = ("powGetSysLoad",)
KEY_SYS_GRID_W = ("powGetSysGrid",)

# Panel / inverter keys (Stream AC Pro, PowerStream, Smart Home Panel).
# Order matters: the FIRST key found wins. `gridConnectionPower` is the
# real grid power on the Stream CA Pro; `powGetSysGrid` exists too but is
# a different (always-0 on that firmware) metric, so we try the real one first.
KEY_PANEL_GRID_W = (
    "gridConnectionPower",   # Stream CA Pro (real grid power, 100-300W typical)
    "powGetSysGrid",         # Some firmware variants
    "gridPower",
    "gridPowerW",
    "powGetGrid",
)
KEY_PANEL_SOLAR_W = (
    "powGetPvSum",           # Most Stream / Ultra X firmwares
    "pvTotalPower",
    "powGetPv",
    "solarPower",
)
KEY_PANEL_LOAD_W = (
    "powGetSysLoad",         # Stream / Ultra X
    "sysLoadPower",
    "powGetLoad",
    "loadPower",
)
KEY_PANEL_BATTERY_W = (
    "powGetBpCms",           # Stream CA Pro
    "powGetBp",              # Stream Ultra X
    "bmsBmsStatus.chargePower",  # Delta fallback
)
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


def _read_with_key(quota: dict[str, Any], keys: tuple[str, ...]) -> tuple[Any | None, str | None]:
    """Like `_read` but also returns the matched key (for debug attributes)."""
    for k in keys:
        if k in quota:
            return quota[k], k
    return None, None


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


# ----------------------------------------------- hybrid extras (PV1-PV4 inputs)
# Some battery devices also expose solar MPPT inputs (Stream Ultra X has 4
# strings). They are added to the battery profile so hybrid devices get full
# coverage without a separate "hybrid" profile. Plain battery devices (Delta,
# River) just won't have these keys and the sensors will report "No disponible".
PV_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="pv1_power",
        translation_key="pv1_power",
        name="PV1 Input",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:solar-panel",
    ),
    SensorEntityDescription(
        key="pv2_power",
        translation_key="pv2_power",
        name="PV2 Input",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:solar-panel",
    ),
    SensorEntityDescription(
        key="pv3_power",
        translation_key="pv3_power",
        name="PV3 Input",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:solar-panel",
    ),
    SensorEntityDescription(
        key="pv4_power",
        translation_key="pv4_power",
        name="PV4 Input",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:solar-panel",
    ),
    SensorEntityDescription(
        # Aggregated solar total. Useful for devices where individual MPPT
        # inputs aren't exposed in the Open API (Stream series only returns
        # `powGetPvSum`, no per-string breakdown).
        key="pv_total_power",
        translation_key="pv_total_power",
        name="PV Total",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:solar-panel-large",
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


# ------------------------------------------------------- diagnostic sensors (all)
# Always created, regardless of detected profile. They show what the integration
# actually saw from the EcoFlow API and what profile was picked, so any
# misdetection is visible at a glance from the device page.
DIAGNOSTIC_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="device_model",
        translation_key="device_model",
        name="Device Model",
        icon="mdi:information-outline",
    ),
    SensorEntityDescription(
        key="device_profile",
        translation_key="device_profile",
        name="Device Profile",
        device_class=SensorDeviceClass.ENUM,
        options=[DEVICE_PROFILE_BATTERY, DEVICE_PROFILE_PANEL, DEVICE_PROFILE_HYBRID],
        icon="mdi:tag-outline",
    ),
)


# --------------------------------------------------------------------- platform


def _build_device_info(
    coordinator: EcoFlowStatusCoordinator, sn: str
) -> DeviceInfo:
    """Build HA DeviceInfo for one EcoFlow device using cached metadata.

    Prefers the productName from the device list (set up in __init__.py);
    falls back to whatever is in the quota response, and finally to a generic
    name from the last 6 chars of the SN.
    """
    # Prefer the model from the device list (always present, set up in __init__).
    model_name = None
    if hasattr(coordinator, "device_models"):
        model_name = coordinator.device_models.get(sn)
    quota = coordinator.data.get(sn) if coordinator.data else None
    name = None
    model = None
    sw_version = None
    if quota:
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
    if not name:
        # Last-resort: hard-coded mapping by SN suffix so devices with no
        # productName in the API still get a useful name.
        sn_suffix = sn[-6:].upper() if sn else ""
        if sn_suffix in KNOWN_DEVICE_MODELS:
            name = KNOWN_DEVICE_MODELS[sn_suffix]
        else:
            name = model_name or f"EcoFlow {sn[-6:]}"
    if not model:
        sn_suffix = sn[-6:].upper() if sn else ""
        if sn_suffix in KNOWN_DEVICE_MODELS:
            model = KNOWN_DEVICE_MODELS[sn_suffix]
        else:
            model = model_name or "EcoFlow device"
    return DeviceInfo(
        identifiers={(DOMAIN, sn)},
        manufacturer=MANUFACTURER,
        model=model,
        name=name,
        sw_version=sw_version,
        serial_number=sn,
    )


def _detect_device_profile(
    coordinator: EcoFlowStatusCoordinator, sn: str
) -> str:
    """Return DEVICE_PROFILE_BATTERY or DEVICE_PROFILE_PANEL.

    Detection order (first match wins):
    1. productName from the device list (populated in __init__.py via
       /device/list, since the quota response does not include it).
    2. productName / productDetail / productType / deviceName from the quota.
    3. SN suffix (last 6 chars) matching a known panel SKU — last-resort
       fallback so a missing/garbled productName still classifies correctly.

    Names are normalized (lowercase, spaces/dashes/underscores removed) so
    "Stream AC Pro", "StreamACPro" and "stream-ac-pro" all match.
    """
    import re as _re

    def _norm(s: str) -> str:
        return _re.sub(r"[\s_\-]+", "", s.lower())

    name_candidates: list[str] = []
    name = coordinator.device_models.get(sn) if hasattr(coordinator, "device_models") else None
    if name:
        name_candidates.append(_norm(str(name)))
    # Fallback: if /device/list returned an empty productName, derive a name
    # from the SN-suffix lookup table so the hint matcher still has something
    # to work with (Stream CA Pro would otherwise land on the SN-suffix PANEL
    # fallback instead of being recognised as hybrid).
    sn_suffix = sn[-6:].upper() if sn else ""
    if not name_candidates and sn_suffix in KNOWN_DEVICE_MODELS:
        name_candidates.append(_norm(KNOWN_DEVICE_MODELS[sn_suffix]))
    quota = coordinator.data.get(sn) if coordinator.data else None
    if isinstance(quota, dict):
        for k in ("productName", "productDetail", "productType", "deviceName"):
            v = quota.get(k)
            if isinstance(v, str) and v:
                name_candidates.append(_norm(v))
    norm_hints = tuple(_norm(h) for h in PANEL_PRODUCT_HINTS)
    hybrid_hints = tuple(_norm(h) for h in HYBRID_PRODUCT_HINTS)
    # Check hybrid FIRST so that e.g. "Stream CA Pro" becomes hybrid
    # instead of falling into the generic "streamacpro" panel hint.
    for n in name_candidates:
        for hint in hybrid_hints:
            if hint and hint in n:
                _LOGGER.info(
                    "EcoFlow device %s detected as HYBRID (matched hint '%s' in '%s')",
                    sn, hint, n,
                )
                return DEVICE_PROFILE_HYBRID
    for n in name_candidates:
        for hint in norm_hints:
            if hint and hint in n:
                _LOGGER.info(
                    "EcoFlow device %s detected as PANEL (matched hint '%s' in '%s')",
                    sn, hint, n,
                )
                return DEVICE_PROFILE_PANEL
    # Fallback: known hybrid SN suffixes (checked BEFORE panel so a hybrid is
    # never misclassified). These are the last line of defence when neither
    # the device list nor the quota response include a usable productName.
    if sn_suffix in {s.upper() for s in HYBRID_SN_SUFFIXES}:
        _LOGGER.warning(
            "EcoFlow device %s detected as HYBRID via SN-suffix fallback (suffix=%s). "
            "No productName matched any hint; please report this so the hint list "
            "can be extended.",
            sn, sn_suffix,
        )
        return DEVICE_PROFILE_HYBRID
    if sn_suffix in {s.upper() for s in PANEL_SN_SUFFIXES}:
        _LOGGER.warning(
            "EcoFlow device %s detected as PANEL via SN-suffix fallback (suffix=%s). "
            "No productName matched a known panel hint; please report this so the "
            "hint list can be extended.",
            sn, sn_suffix,
        )
        return DEVICE_PROFILE_PANEL
    if name_candidates:
        _LOGGER.info(
            "EcoFlow device %s detected as BATTERY (no panel hint matched; names tried: %s)",
            sn, name_candidates,
        )
    else:
        _LOGGER.warning(
            "EcoFlow device %s has no usable productName and no SN-suffix match; "
            "defaulting to BATTERY profile.",
            sn,
        )
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
        profile = _detect_device_profile(coordinator, sn)
        device_info = _build_device_info(coordinator, sn)
        if profile == DEVICE_PROFILE_HYBRID:
            # Hybrid = battery + panel + PV1-4 + diagnostics. Used for devices
            # like the Stream CA Pro and Stream Ultra X that carry both a
            # battery and a grid-tie inverter.
            for desc in SENSORS_PER_DEVICE:
                entities.append(EcoFlowSensorEntity(coordinator, sn, desc, device_info))
            for desc in PANEL_SENSORS:
                entities.append(EcoFlowSensorEntity(coordinator, sn, desc, device_info))
            for desc in PV_SENSORS:
                entities.append(EcoFlowSensorEntity(coordinator, sn, desc, device_info))
        elif profile == DEVICE_PROFILE_PANEL:
            for desc in PANEL_SENSORS:
                entities.append(EcoFlowSensorEntity(coordinator, sn, desc, device_info))
        else:
            for desc in SENSORS_PER_DEVICE:
                entities.append(EcoFlowSensorEntity(coordinator, sn, desc, device_info))
            # Plain battery devices may still have PV1-PV4 (Stream Ultra X is
            # in this branch when the productName doesn't match a hybrid hint
            # but the SN suffix does). No harm if the keys are absent.
            for desc in PV_SENSORS:
                entities.append(EcoFlowSensorEntity(coordinator, sn, desc, device_info))
        # Diagnostic sensors are always created (model + detected profile) so
        # any misdetection is immediately visible in HA without digging logs.
        for desc in DIAGNOSTIC_SENSORS:
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
        # Cached for extra_state_attributes so users can see which quota key
        # the integration actually used (or "none" when the value is None).
        self._last_matched_key: str | None = None

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
        # Hybrid PV inputs (Stream Ultra X etc.)
        if key == "pv1_power":
            return _to_float(_read(quota, KEY_PV1_W))
        if key == "pv2_power":
            return _to_float(_read(quota, KEY_PV2_W))
        if key == "pv3_power":
            return _to_float(_read(quota, KEY_PV3_W))
        if key == "pv4_power":
            return _to_float(_read(quota, KEY_PV4_W))
        if key == "pv_total_power":
            return _to_float(_read(quota, KEY_PV_SUM_W))
        # Panel sensors (track which quota key was actually used)
        if key == "grid_power":
            v, k = _read_with_key(quota, KEY_PANEL_GRID_W)
            self._last_matched_key = k
            return _to_float(v)
        if key == "solar_power":
            v, k = _read_with_key(quota, KEY_PANEL_SOLAR_W)
            self._last_matched_key = k
            return _to_float(v)
        if key == "system_load":
            v, k = _read_with_key(quota, KEY_PANEL_LOAD_W)
            self._last_matched_key = k
            return _to_float(v)
        if key == "panel_battery_power":
            v, k = _read_with_key(quota, KEY_PANEL_BATTERY_W)
            self._last_matched_key = k
            return _to_float(v)
        if key == "feed_in_mode":
            v, k = _read_with_key(quota, KEY_PANEL_FEED_IN_MODE)
            self._last_matched_key = k
            mode = _to_float(v)
            return int(mode) if mode is not None else None
        if key == "backup_reserve_soc":
            v, k = _read_with_key(quota, KEY_PANEL_BACKUP_SOC)
            self._last_matched_key = k
            return _to_float(v)
        if key == "energy_strategy":
            sp, k1 = _read_with_key(quota, ("energyStrategyOperateMode.operateSelfPoweredOpen",))
            sch, k2 = _read_with_key(quota, ("energyStrategyOperateMode.operateIntelligentScheduleModeOpen",))
            self._last_matched_key = k1 or k2
            if sch is True or sch == "1" or sch == 1:
                return "Scheduled"
            if sp is True or sp == "1" or sp == 1:
                return "Self-powered"
            return "Standard"
        # Diagnostic sensors (always present regardless of profile)
        if key == "device_model":
            # Prefer the cached productName from the device list (more reliable
            # than the quota, which often omits it for Stream-series devices).
            model = self.coordinator.device_models.get(self._sn)
            if model:
                return model
            for k in ("productName", "productDetail", "productType", "deviceName"):
                v = quota.get(k)
                if isinstance(v, str) and v:
                    return v
            # Last resort: hard-coded mapping by SN suffix (EcoFlow doesn't
            # always include productName in the API responses for Stream devices).
            sn_suffix = self._sn[-6:].upper() if self._sn else ""
            if sn_suffix in KNOWN_DEVICE_MODELS:
                return KNOWN_DEVICE_MODELS[sn_suffix]
            return None
        if key == "device_profile":
            return _detect_device_profile(self.coordinator, self._sn)
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the full raw quota + matched key for debugging key mismatches.

        - `matched_key`: the quota key the integration actually used to compute
          this sensor's value (e.g. `gridConnectionPower`). `None` when the
          value is `None` (key not found in the quota).
        - `raw_quota_sample`: first 30 entries of the quota response, enough
          to see all relevant keys for any device family.
        """
        quota = self.coordinator.data.get(self._sn) if self.coordinator.data else None
        if not quota or not isinstance(quota, dict):
            return {"matched_key": self._last_matched_key, "raw_quota_sample": None}
        items = list(quota.items())[:30]
        return {
            "matched_key": self._last_matched_key,
            "raw_quota_sample": dict(items),
        }
