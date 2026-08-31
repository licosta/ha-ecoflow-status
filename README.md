# EcoFlow Status — Home Assistant Custom Integration

A focused Home Assistant integration for the **EcoFlow IoT Open Platform** that
exposes the battery state, charge power, discharge power, state of charge and
state of health of each paired device.

This is **not** a replacement for the existing
`tolwi/hassio-ecoflow-cloud`, `rabits/ha-ef-ble` or
`shuette42/ecoflow-energy-ha` integrations — those cover much more. This one
does one thing well: **tells you, at a glance, whether each EcoFlow battery is
charging, discharging or idle, and at what rate**, with a stable state sensor
you can use in dashboards and automations.

Tested for **Stream CA Pro** and **Stream Ultra X**, but the same quota keys
work for the entire Stream / Delta series. Other product lines (Delta Pro,
Power Kit, PowerStream) should also work as long as they expose
`bmsBmsStatus.chargePower` / `bmsBmsStatus.dischargePower` /
`bmsBmsStatus.soc` / `bmsBmsStatus.soh` in their quota response.

## Sensors exposed per device

| Entity | Device class | State class | Unit | Notes |
|---|---|---|---|---|
| `sensor.<name>_state` | enum | — | — | `Charging` / `Discharging` / `Standby` |
| `sensor.<name>_charge_power` | power | measurement | W | Current charging power |
| `sensor.<name>_discharge_power` | power | measurement | W | Current discharging power |
| `sensor.<name>_soc` | battery | measurement | % | State of charge |
| `sensor.<name>_soh` | battery | measurement | % | State of health |

`<name>` is the last 6 characters of the device serial number, so entity_ids
stay short and unique. You can rename them freely via the UI — the
`unique_id` is the full SN + key, so re-renaming is safe.

## Install (manual, no HACS required)

1. Get your **Access Key** and **Secret Key** from
   <https://developer-eu.ecoflow.com> (EU) or <https://developer.ecoflow.com>
   (global). The developer account must be approved (usually 3–7 days) and
   the serial numbers of your devices must be **whitelisted** in the
   developer portal (EcoFlow's policy).
2. Copy the `custom_components/ecoflow_status` folder to your Home Assistant
   `config/custom_components/` directory:

   ```text
   /config/custom_components/ecoflow_status/
   ├── __init__.py
   ├── manifest.json
   ├── const.py
   ├── api.py
   ├── coordinator.py
   ├── config_flow.py
   ├── sensor.py
   └── translations/
       ├── en.json
       └── es.json
   ```

3. Restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → search "EcoFlow
   Status"**.
5. Pick the region (EU by default), paste the Access Key and Secret Key,
   then select the devices you want to track.

## Install (HACS)

If you want to install via HACS instead, push this folder to a public GitHub
repo and add it as a **Custom repository** (category: *Integration*). HACS
will then offer it under "EcoFlow Status".

A minimal `hacs.json` at the repo root would look like:

```json
{
  "name": "EcoFlow Status",
  "render_readme": true,
  "homeassistant": "2025.4.0"
}
```

## How the state is derived

The `state` sensor reads `bmsBmsStatus.chargePower` and
`bmsBmsStatus.dischargePower` and applies a 5 W threshold (so the BMS
self-consumption noise doesn't flip the state):

| chargePower | dischargePower | state |
|---|---|---|
| > 5 W | ≤ 5 W | `Charging` |
| ≤ 5 W | > 5 W | `Discharging` |
| otherwise | | `Standby` |

If EcoFlow ever changes the key names, edit the `KEY_*` tuples at the top of
`sensor.py` — that's the only place the keys are referenced.

## How the API auth works

Every request is signed with **HMAC-SHA256** over the alphabetically sorted
request parameters concatenated with `accessKey`, `nonce` and `timestamp`,
keyed by the `secretKey`. Headers sent on every call:

```text
accessKey: <your-access-key>
nonce:     <6-digit-random>
timestamp: <unix-ms>
sign:      <hex-hmac-sha256>
Content-Type: application/json;charset=UTF-8
```

Sign strings are computed differently for GET vs POST:

- **GET** (`/iot-open/sign/device/list`, `/iot-open/sign/device/quota/all`):
  the query parameters (if any) are sorted, joined with `&`, then
  `accessKey=&nonce=&timestamp=` is appended and the result is HMAC'd.
- **POST** (`/iot-open/sign/device/quota`): the JSON body is flattened with
  dotted keys, sorted, joined the same way, and HMAC'd.

The implementation lives in `api.py` (see `_sign` and `_request`). No tokens
are stored — every request re-signs. There's no rate-limit cache either, so
keep the polling interval at 30 s or higher (default).

## Quota response shape (Stream series)

The relevant keys returned by `quota/all` look like this (units confirmed
for CA Pro and Ultra X via the HACS community integrations):

```json
{
  "bmsBmsStatus.soc": 40,
  "bmsBmsStatus.soh": 100,
  "bmsBmsStatus.chargePower": 85,
  "bmsBmsStatus.dischargePower": 0,
  "bmsBmsStatus.inputWatts": 85,
  "bmsBmsStatus.outputWatts": 0,
  "bmsBmsStatus.remainTime": 1234
}
```

If your device uses milliwatts instead of watts (older firmware or different
product line), you'll see charges of `85000` instead of `85`. The integration
trusts whatever the API returns, so either fix it in the ESPHome-style
firmware (if it's an EcoFlow device) or edit the `_to_watts` helper in
`sensor.py` to divide by 1000.

## Troubleshooting

- **`Authentication failed`** — re-check the Access Key and Secret Key. Make
  sure the device serial number has been **whitelisted** in the EcoFlow
  developer portal under "My devices".
- **`Cannot reach EcoFlow`** — wrong region (try the other one), or your
  network is blocking `api-e.ecoflow.com` / `api.ecoflow.com`. The integration
  also requires outbound HTTPS, which most HA installs allow by default.
- **Sensors stay `--` / `unavailable`** — check **Developer Tools → Logs**
  and filter by `custom_components.ecoflow_status`. The most common cause is
  the integration starting before the aiohttp session is ready; a second
  reload fixes it.
- **State stuck in `Standby`** — confirm your device actually reports a
  non-zero `bmsBmsStatus.chargePower` in the raw quota (Settings → Devices &
  Services → EcoFlow Status → *device* → **Developer Tools → YAML** and run
  `homeassistant.helpers.template` or use the `sensor.*_charge_power` to
  read the underlying value).

## Limitations

- **No control**: this is a read-only integration. To turn AC on/off, set
  charge limits, etc., use one of the HACS integrations above or send a
  `set` command to the MQTT topic (the API supports it but it's not
  implemented here — PRs welcome).
- **HTTP polling, not MQTT**: the Open API HTTP path is the simplest. If you
  need sub-second latency, use `tolwi/hassio-ecoflow-cloud` which subscribes
  to the MQTT cert flow.
- **No cycle count, no cell voltage, no temps**: the brief was state + power
  only. Add more `SensorEntityDescription` entries to `sensor.py` if you
  need them.

## License

MIT.
