# EcoFlow Status

A focused Home Assistant integration for **EcoFlow batteries** (Stream CA Pro,
Stream Ultra X and the rest of the Stream / Delta series) that exposes the
battery state, charge power, discharge power, state of charge and state of
health for each paired device.

This integration is intentionally small and read-only. It uses the **EcoFlow
IoT Open Platform** HTTP API and works alongside the larger HACS integrations
(`tolwi/hassio-ecoflow-cloud`, `rabits/ha-ef-ble`, `shuette42/ecoflow-energy-ha`)
without duplicating their work.

## Sensors per device

- **State** (`charging` / `discharging` / `standby`)
- **Charge Power** (W)
- **Discharge Power** (W)
- **Battery** (SoC %)
- **State of Health** (SoH %)

## Setup

1. Get an **Access Key** and **Secret Key** from
   <https://developer-eu.ecoflow.com> (EU) or <https://developer.ecoflow.com>
   (global). The serial numbers of your devices must be **whitelisted** in
   the developer portal.
2. In Home Assistant: **Settings → Devices & Services → Add Integration →
   search "EcoFlow Status"**.
3. Pick the region (EU by default), paste the keys, then select the devices
   you want to track.

## Documentation

See the full [README](README.md) for troubleshooting, the state-derivation
logic, the API auth scheme, and known limitations.
