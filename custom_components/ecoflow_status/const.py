"""Constants for the EcoFlow Status integration."""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "ecoflow_status"
MANUFACTURER: Final = "EcoFlow"
DEFAULT_NAME: Final = "EcoFlow Status"

# Configuration keys
CONF_ACCESS_KEY: Final = "access_key"
CONF_SECRET_KEY: Final = "secret_key"
CONF_REGION: Final = "region"
CONF_DEVICES: Final = "devices"
CONF_SCAN_INTERVAL: Final = "scan_interval"

# Regions / API base URLs
REGION_EU: Final = "eu"
REGION_GLOBAL: Final = "global"
REGION_NA: Final = "na"
REGIONS: Final[dict[str, str]] = {
    REGION_EU: "https://api-e.ecoflow.com",
    REGION_GLOBAL: "https://api.ecoflow.com",
    REGION_NA: "https://api-a.ecoflow.com",
}
DEFAULT_REGION: Final = REGION_EU

# Endpoints
PATH_DEVICE_LIST: Final = "/iot-open/sign/device/list"
PATH_DEVICE_QUOTA_ALL: Final = "/iot-open/sign/device/quota/all"

# State thresholds
# Below this, charge/discharge power is treated as 0 (BMS self-consumption)
POWER_THRESHOLD_W: Final = 5.0

# Battery states
STATE_CHARGING: Final = "charging"
STATE_DISCHARGING: Final = "discharging"
STATE_STANDBY: Final = "standby"
STATES: Final[tuple[str, ...]] = (STATE_CHARGING, STATE_DISCHARGING, STATE_STANDBY)

# Defaults
DEFAULT_SCAN_INTERVAL: Final = 30
MIN_SCAN_INTERVAL: Final = 10
MAX_SCAN_INTERVAL: Final = 300

# Device profiles (battery vs panel/inverter). Detected from productName in
# the device list response. Battery devices get the 8 battery sensors; panel
# devices get the 7 panel sensors. Stream AC Pro is a panel, not a battery.
# Hints are matched after normalizing the productName (lowercase, spaces/dashes
# removed) so e.g. "Stream AC Pro", "StreamACPro", "stream-ac-pro" all match.
DEVICE_PROFILE_BATTERY: Final = "battery"
DEVICE_PROFILE_PANEL: Final = "panel"
PANEL_PRODUCT_HINTS: Final[tuple[str, ...]] = (
    "streamacpro",
    "streamac",
    "acp",
    "smarthomepanel",
    "smarthome",
    "powerstream",
    "microinverter",
    "inverter",
    "balcony",  # EU "Balkonkraftwerk" terminology
    "micro",    # generic micro-inverter hint
)
# SN suffixes (last 6 chars) known to be panel-class devices. Used as a last
# resort when the device list returns no usable productName for a device.
PANEL_SN_SUFFIXES: Final[tuple[str, ...]] = (
    "1N0006",   # Stream AC Pro (verified)
)

# SN-suffix (last 6 chars, uppercase) -> human-readable model name. Used as a
# last-resort fallback for the `device_model` diagnostic sensor when neither
# /device/list nor the quota response include a productName. Extend as new
# devices are verified.
KNOWN_DEVICE_MODELS: Final[dict[str, str]] = {
    "1N0006": "Stream AC Pro",
    "5L0686": "Stream Ultra X",
}
