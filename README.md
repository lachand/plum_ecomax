# Plum ecoMAX for Home Assistant
[![CI](https://github.com/lachand/plum_ecomax/actions/workflows/ci.yml/badge.svg)](https://github.com/lachand/plum_ecomax/actions/workflows/ci.yml)
[![Documentation](https://github.com/lachand/plum_ecomax/actions/workflows/doc.yaml/badge.svg)](https://github.com/lachand/plum_ecomax/actions/workflows/doc.yaml)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

**Plum ecoMAX** is a custom integration for Home Assistant that allows you to monitor and control Plum ecoMAX boiler controllers via a network connection (RS485/Ethernet).

{% if installed %}
## Getting Started

Once installed via HACS, you can set up the integration via the Home Assistant generic **Settings** > **Devices & Services** > **Add Integration** menu. Search for "Plum ecoMAX".
{% endif %}

## Features

This integration aims to support the primary functions of the ecoMAX controller:

* **Monitoring:** Read current temperatures (feeder, boiler, outside, etc.), boiler status, and fuel consumption.
* **Control:** Adjust target temperatures and operation modes, per-circuit heating curves, cooling setpoints, the DHW anti-legionella cycle, and the DHW circulation pump timing.
* **Weekly schedule:** Read the comfort/eco weekly program per circuit and for DHW as a calendar, and rewrite it with the **`plum_ecomax.set_schedule`** service (target day(s), one or more comfort time ranges — everything else becomes eco).
* **Manual mode & solar dump:** The **DHW pump → Solar buffer** switch enters manual mode, forces the DHW transfer pump, and (when turned off) stops it and leaves manual mode — so it actually does something, unlike a bare force parameter. The **`plum_ecomax.solar_to_buffer`** service does the same on a capped timer, and a **Solar dump automatic mode** switch runs it as a self-regulating differential-temperature controller (bank solar-heated DHW into the buffer tanks all summer, with circulator-runtime guard rails). A plain **Manual mode** switch (operating-mode register) is also exposed. All paths guarantee a return to automatic — on the timer, on reload, on HA shutdown. See *A note on "force" parameters* below.
* **Sensors:** Binary sensors for pumps and fan status, plus the boiler's serial number (shown on the device page) and any circuit names configured on the physical panel.
* **Diagnostics & alerts:** A "manual mode active" binary sensor, coarse alarm-bit indicators, "last communication" / "consecutive failures" link-health sensors, and repair issues in Settings → Repairs when an alarm is active, a write is rejected by the boiler, or the connection is lost.
* **Reference values:** Buttons to save the current heating-curve/DHW configuration as a reference snapshot and restore it later.
* **Honest polling:** live telemetry (temperatures, pump/alarm state) is re-read every polling cycle, so the configured interval actually bounds how stale a reading can be; setpoints and schedules are cached longer.
* **Batched, persistent connection:** Several parameters are read per network request instead of one connection per value, and the TCP connection to the boiler is kept open across polls (with automatic reconnection) instead of reconnecting every time.
* **Diagnostics download:** Downloadable redacted diagnostics snapshot from the device page (Settings → Devices & Services → Plum EcoMAX → Download diagnostics).
* **Reconfigurable:** IP address, port, credentials, active circuits, and polling interval can be changed later via **Reconfigure** on the integration, without deleting and re-adding it.

### A note on "force" parameters

The boiler applies a *force* parameter (`hdwpumpforce`, and the front-panel forces in general) only while the controller is in **manual mode** (operating-mode register = manual). Outside manual mode the boiler's automatic control silently overrides the forced value. The "Manual mode active" binary sensor reflects whether that condition is currently met.

That's why the **DHW pump → Solar buffer** switch and the `solar_to_buffer` service both drive manual mode for you: switch on / service call → enter manual mode, then force the pump; switch off / timer expiry → stop the pump, then leave manual mode (only if they were the one that entered it — if you flipped the **Manual mode** switch yourself, they leave it alone). If a return to automatic ever fails they raise a repair issue in **Settings → Repairs**.

**Temperature guard rails** (so a transfer doesn't drain the DHW tank too far): two number entities in the boiler device's Configuration section —

* **Solar dump start temperature** (default **50 °C**): a transfer won't start if the DHW tank is already below this. If it would have started (e.g. from an automation), you get a persistent notification instead.
* **Solar dump stop temperature** (default **42 °C**): a running transfer stops once the DHW tank drops to this.

The `solar_to_buffer` service also takes optional `start_temp` / `stop_temp` to override them for a single call.

While manual mode is on, the boiler's automatic regulation is disabled — the plain **Manual mode** switch is there if you want it directly, just remember to turn it back off.

### Automatic mode

The **Solar dump automatic mode** switch turns the manual transfer into a self-running **differential-temperature controller** — set it on in summer / shoulder season and leave it. It works in short **bursts** (enter manual mode → run the pump → back to automatic), so heating and the gas DHW backup keep working normally between bursts.

Each tick (every 2.5 min) it compares the DHW tank (`tempcwu`) and the buffer (`tempbufordown`):

* **Charge** (buffer below its target): start a burst when the DHW tank is hotter than the buffer by **ΔT to start** (default 8 K), stop the burst when the gap falls below 3 K.
* **Balance** (buffer at/above its target): only burst on a gap of **2× ΔT-start** (16 K) — so once the buffer is charged, the pump only skims the big surpluses and both tanks drift up together.

Guard rails: never pull the DHW tank below the **auto DHW floor** (default 45 °C); stop above a 75 °C buffer ceiling; a **circulator budget per day** (default 120 min, 0 = unlimited) hard-caps runtime; bursts are skipped during the anti-legionella hour; minimum 5 min run / 12 min rest stop the pump short-cycling. The **Solar dump circulator runtime today** sensor shows how much the pump has run — use it to tune the thresholds. All four knobs are number entities in the boiler's Configuration section.

### Example: dump solar heat to the buffer before an overcast day

```yaml
automation:
  - alias: Solar dump before overcast
    trigger:
      - trigger: numeric_state
        entity_id: sensor.solar_forecast_tomorrow_kwh   # your own forecast sensor
        below: 5
    condition:
      - condition: numeric_state
        entity_id: sensor.plum_ecomax_dhw_temperature   # solar tank is actually hot
        above: 60
    action:
      - action: plum_ecomax.solar_to_buffer
        data:
          duration: 45
```

## Installation

### Option 1: HACS (Recommended)
1. Open HACS in your Home Assistant instance.
2. Go to "Integrations" and click the three dots in the top right corner.
3. Select "Custom repositories".
4. Add the URL of this repository: `https://github.com/lachand/plum_ecomax`.
5. Select **Integration** as the category.
6. Click **Add** and then install the integration.
7. **Restart Home Assistant.**

### Option 2: Manual Installation
1. Download the `plum_ecomax` directory from the `custom_components` folder in this repository.
2. Copy the directory into your Home Assistant `<config>/custom_components/` directory.
3. **Restart Home Assistant.**

## Configuration

Configuration is done via the **User Interface (Config Flow)**.
You will need to provide:
* **IP Address:** The local IP address of your ecoMAX module.
* **Port:** The port used for communication (default usually 8899).
* **Username / Password:** Admin credentials for the boiler.
* **Active Heating Circuits:** Only the circuits you select here get entities — unused circuits (1-7) in the boiler's parameter catalog are skipped.
* **Polling interval:** How often (in seconds, 10-300) the integration reads from the boiler. Defaults to 30s.

Any of these can be changed later via **Reconfigure** on the integration card, without removing it.

## Development

Unit and regression tests live in `tests/` (`pytest tests/`). CI runs them on a Python 3.13/3.14 matrix (matching the Home Assistant releases users actually run) alongside `ruff check` / `ruff format --check`, `hassfest`, and HACS validation. Minimum supported Home Assistant: **2025.2**. See `DP_INVENTORY.md` for the catalog of boiler parameters not yet exposed as entities.

### Upgrading to 0.7.0

Cleanup release, no new entities. **Behaviour changes:** the integration's **Configure** button is gone — use **Reconfigure** (⋮ menu on the integration) to change IP, port, credentials, circuits or polling interval. Entity **names** on the *Circuit N* and *DHW* devices no longer repeat the device name (e.g. "Circuit 1 Base Temperature" is now "Base Temperature" under the device *Circuit 1*, still displayed as "Circuit 1 Base Temperature"); existing entity IDs are unchanged. **Devices that no longer exist** (a circuit that is no longer active, a device left with no entity) are removed automatically at startup, together with their orphaned entities, and can be deleted from the UI. Under the hood: solar-dump state is kept per config entry, frames and parameters are typed, and `mypy` runs in CI. Restart Home Assistant after updating.

### Upgrading to 0.6.0

Robustness release, no new entities. **Behaviour changes:** when the boiler is unreachable, entities now go *unavailable* (previously they kept showing the last cached value) and the outage is logged once instead of every cycle; if the boiler is unreachable at startup, the integration retries instead of loading empty. Config entries are now keyed by the boiler's **serial number** instead of its IP — existing entries are converted automatically at the next start, and a changed IP no longer creates a duplicate (use **Reconfigure** to point an entry at a new address; it refuses a different boiler). The boiler link moved to asyncio streams with per-transaction timeouts. A wrong password is still only detected on the first write — reads never send it. Restart Home Assistant after updating.

### Upgrading to 0.5.0

Adds **Solar dump automatic mode** — a differential-temperature controller that runs the DHW → buffer transfer by itself (see *Automatic mode* above), plus its four Configuration number entities (auto DHW floor, buffer target, ΔT to start, circulator budget per day) and a **circulator runtime today** sensor. New entities + translations need a full HA restart to show. No breaking changes; the auto switch is off until you turn it on.

### Upgrading to 0.4.x

Adds the **Manual mode** switch, the **DHW pump → Solar buffer** switch (which now drives manual mode itself), the `plum_ecomax.solar_to_buffer` service, and the **Solar dump start/stop temperature** number entities — see *A note on "force" parameters* above. The temperature guard rails are **active by default** (start 50 °C / stop 42 °C): a `solar_to_buffer` call or a switch-on with the DHW tank below 50 °C no longer runs the pump — adjust the numbers to taste. No other breaking changes.

### Upgrading to 0.3.0

The `detectalarmstate` register is no longer a "problem" binary sensor (it read as a permanent false alarm on real hardware) — it's now a plain diagnostic sensor. The old `binary_sensor.*_detectalarmstate` entity becomes unavailable after restart; remove it from the entity registry, or delete and re-add the integration, to clear it.

## Disclaimer

This integration is developed by the community and is **not** officially affiliated with or endorsed by Plum Sp. z o.o. Use it at your own risk.

---
If you encounter any issues, please open a ticket on [GitHub Issues](https://github.com/lachand/plum_ecomax/issues).
