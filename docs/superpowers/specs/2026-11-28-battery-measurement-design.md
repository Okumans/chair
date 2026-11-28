# Battery measurement — design

Stacked on `feat/mqtt-library`; merges as its own PR after that one.

## Intent

The protocol, JSON, MQTT schema and Pi bridge already carry `battery_mv` and a `BATTERY_LOW`
event, but the sensor always reports "unavailable". Make the chair report its real battery
voltage and raise `BATTERY_LOW` once when the cell runs down. The reading itself was already
proven on a bench demo (plain ADC read through the divider).

## Goals / non-goals

- Goal: real `battery_mv` in every telemetry frame; `BATTERY_UNAVAILABLE` cleared when a reading
  exists.
- Goal: one `BATTERY_LOW` per discharge, not one per heartbeat or wake.
- Non-goal: switching the divider off during sleep (it draws ~20 µA). A later hardware change.
- Non-goal: percentage / state-of-charge estimation; the Pi gets millivolts.
- Non-goal: any wire, JSON, MQTT or bridge change — the fields already exist.

## Hardware

One 21700 Li-ion cell, 5000 mAh (4.2 V full, ~3.6 V nominal) → 100k / 100k divider → GPIO0 (`Pins::BATTERY_ADC`). Cell mV =
`analogReadMilliVolts(GPIO0) × 2` (ESP32-C3 eFuse calibration, no extra calibration).

## Approaches

- **A. Engine owns the value and the low alert (picked).** Adapter samples the ADC and hands
  mV to the engine, like `imuAvailable`. Engine fills telemetry and runs the low/re-arm logic.
  Host-testable.
- B. Reading only, no `BATTERY_LOW`. Smaller, but leaves the event type dead and pushes
  threshold logic to the Pi.
- C. Separate battery module in `shared/` queried by the engine. Too much structure for one pin.

## Design

- **Engine** (`shared/`): new `void battery(uint16_t mv)`; stores the latest mV (default
  `UINT16_MAX`). `frame()` uses it and sets `BATTERY_UNAVAILABLE` only while it is
  `UINT16_MAX`. Public `bool batteryLow` latch: below `LOW_MV` and not latched → latch and
  `queueEvent(BATTERY_LOW)`; at or above `REARM_MV` → clear the latch. `UINT16_MAX` never
  changes the latch.
- **Constants** (`chair_constants.h`, `Battery` namespace): `LOW_MV = 3400`,
  `REARM_MV = 3600`, `MIN_VALID_MV = 1000`, `SAMPLE_INTERVAL_MS = 60000`, `SAMPLES = 8`.
- **Sensor adapter** (`sensor/src/main.cpp`): average `SAMPLES` reads, ×2; below
  `MIN_VALID_MV` (no cell / USB only) → `UINT16_MAX`. Read once in `setup()` (covers every
  wake, since deep sleep restarts through `setup()`) and then every `SAMPLE_INTERVAL_MS` in
  `loop()`. `batteryLow` is kept in `RTC_DATA_ATTR` like `retainedSeq`, restored into the
  engine at start and saved before deep sleep, so a low cell does not re-alert on every timer
  wake.
- **Tests**: native engine tests for value in frames, health flag, single alert, hysteresis
  re-arm, unavailable reading not touching the latch.
- **Docs**: drop "battery deferred" from README, PROTOCOL_SPEC, SETUP; note the divider and
  thresholds in SETUP.

## Calls made by Claude (correct if wrong)

- Thresholds 3400 / 3600 mV for the 21700 Li-ion cell: 3.4 V leaves roughly 10 % at this
  chair's light load, well above the ~2.5–3.0 V cutoff; 200 mV of hysteresis absorbs sag and
  recovery around radio bursts.
- 1000 mV floor treats "no cell connected" as unavailable rather than reporting ~0 mV.
- Latch lives in RTC memory, not NVS: lost on power-off, which re-alerts once after a cold
  boot on a still-low cell. Acceptable.
- 60 s sampling while awake; 8-sample average.
