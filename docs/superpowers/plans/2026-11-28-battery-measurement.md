# Battery Measurement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The sensor reports its real 21700 cell voltage in `battery_mv` and sends `BATTERY_LOW` once per discharge.

**Architecture:** The sensor adapter (`sensor/src/main.cpp`) averages ADC reads through the ÷2 divider and passes millivolts to `Engine::battery()`. The engine (`shared/`) stamps the value into every frame's common telemetry, and in `tick()` runs a low/re-arm latch that queues a reliable `BATTERY_LOW`. The latch is mirrored in RTC memory across deep sleep.

**Tech Stack:** C++23, Arduino-ESP32 (ESP32-C3), native host tests run through `tests/test_transport.py`.

**Spec:** `docs/superpowers/specs/2026-11-28-battery-measurement-design.md`

## Global Constraints

- Branch `feat/battery-measure`, stacked on `feat/mqtt-library`. No wire, JSON, MQTT schema or bridge changes.
- Cell mV = `analogReadMilliVolts(Pins::BATTERY_ADC) × 2` (100k/100k divider, GPIO0).
- `LOW_MV = 3400`, `REARM_MV = 3600`, `MIN_VALID_MV = 1000`, `SAMPLE_INTERVAL_MS = 60000`, `SAMPLES = 8`.
- Unavailable is `UINT16_MAX` (65535); `Health::BATTERY_UNAVAILABLE` set only while unavailable.
- Low means strictly `< LOW_MV`; re-arm means `>= REARM_MV`.
- Pre-commit runs clang-format, ruff and the full unittest suite; commits must pass it.

## Review Focus

1. Voltage chattering around 3400 mV (3390 → 3410 → 3390) must alert once, not three times — Task 1 test `chatter`.
2. Exact boundaries: 3400 is not low; 3600 re-arms, 3599 does not — Task 1 test `boundaries`.
3. No cell / USB only (ADC near 0) must report unavailable and leave the latch untouched — Task 1 test `unavailable`, Task 2 test `noCell`.
4. A timer wake with the cell still low and the latch retained must not re-alert — Task 2 test `timerWakeKeepsLatch`.
5. On cold boot with a low cell, `BATTERY_LOW` must come after the startup `STATE_SYNC`, never before it — Task 1 test `afterStartup`.

---

### Task 1: Engine battery value and low alert

**Files:**
- Modify: `shared/include/chair_constants.h` (add `namespace Battery` after `Health`)
- Modify: `shared/include/chair_engine.h` (public API + private field)
- Modify: `shared/src/chair_delivery.cpp:17-19` (`Engine::frame` common telemetry)
- Modify: `shared/src/chair_engine.cpp` (`Engine::tick`, ~line 244)
- Create: `tests/protocol_battery_test.cpp`
- Modify: `tests/test_transport.py:88-91` (add `"protocol_battery_test.cpp"` to the set that links `ENGINE_SOURCES`)

**Interfaces:**
- Produces:
  - `SmartChair::Battery::{LOW_MV, REARM_MV, MIN_VALID_MV}` (`uint16_t`), `SAMPLE_INTERVAL_MS` (`uint32_t`), `SAMPLES` (`uint8_t`), all `inline constexpr`.
  - `void Engine::battery(uint16_t mv);` — stores the latest reading; `UINT16_MAX` = unavailable.
  - `bool Engine::batteryLow = false;` — public latch, restored/saved by the adapter.
  - `uint16_t Engine::batteryMv() const;` — latest stored reading.

- [ ] **Step 1: Write the failing tests** in `tests/protocol_battery_test.cpp`, reusing the helpers pattern from `tests/protocol_v2_engine_test.cpp` (`save`, `id()`, `nextOf`, `ack`). Bring the engine out of `STARTING` with `e.seat(false, 0); e.seat(false, 50);` and drain/ack frames as needed. Cases, each in its own function called from `main`:
  - `defaults`: fresh engine, any frame → `common.battery == UINT16_MAX` and `common.health & Health::BATTERY_UNAVAILABLE`.
  - `reported`: `e.battery(3910)` → next frame has `common.battery == 3910` and `!(health & BATTERY_UNAVAILABLE)`; `IMU_UNAVAILABLE` bit still follows `imuAvailable`.
  - `alertsOnce`: `battery(3300); tick(t)` → exactly one `BATTERY_LOW` queued, `e.batteryLow`; further `tick`s with 3300 queue none.
  - `chatter`: 3390 → tick → 3410 → tick → 3390 → tick → one `BATTERY_LOW` total.
  - `boundaries`: 3400 → tick → no alert. Then 3399 → alert; 3599 → tick → `batteryLow` still true; 3600 → tick → false; 3399 → second alert.
  - `unavailable`: latch set, then `battery(UINT16_MAX); tick` → `batteryLow` unchanged, no alert; latch clear + unavailable → no alert.
  - `restoredLatch`: `e.batteryLow = true` before startup, `battery(3300)` → no alert.
  - `afterStartup`: `battery(3300)` before the seat debounce; ticks during `STARTING` queue nothing; after debounce the `STATE_SYNC`'s `id.seq` < the `BATTERY_LOW`'s `id.seq`.

  Count alerts by draining `e.next(...)` and counting `Type::BATTERY_LOW` (ack each reliable frame so the outbox empties).

- [ ] **Step 2: Add the file to the engine-linked set in `tests/test_transport.py` and run to see it fail**

Run: `uv run --locked python -m unittest tests.test_transport -k test_protocol -v`
Expected: FAIL — compile errors for `Engine::battery` / `batteryLow` / `Battery::`.

- [ ] **Step 3: Implement**
  - Constants as listed above in `namespace Battery` (doc comment: single 21700 Li-ion cell through a 100k/100k divider).
  - `Engine`: private `uint16_t batteryMv_ = UINT16_MAX;`. `battery(mv)` only stores.
  - `frame()`: `common.battery = batteryMv_`; set `BATTERY_UNAVAILABLE` only when `batteryMv_ == UINT16_MAX`.
  - `tick()`: when `operating != Operating::STARTING` and `batteryMv_ != UINT16_MAX`: if `!batteryLow && batteryMv_ < LOW_MV` → `batteryLow = true; queueEvent(Type::BATTERY_LOW, now);` else if `batteryLow && batteryMv_ >= REARM_MV` → `batteryLow = false`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --locked python -m unittest tests.test_transport -v`
Expected: all pass (existing tests unchanged — they never call `battery()`, so frames stay unavailable).

- [ ] **Step 5: Commit**

```bash
git add shared/include/chair_constants.h shared/include/chair_engine.h shared/src/chair_delivery.cpp shared/src/chair_engine.cpp tests/protocol_battery_test.cpp tests/test_transport.py
git commit -m "feat: report battery voltage and alert once when low"
```

### Task 2: Sensor ADC reading and retained latch

**Files:**
- Modify: `tests/fakes/Arduino.h` (declare `uint32_t analogReadMilliVolts(uint8_t pin);`)
- Modify: `tests/sensor_sleep_test.cpp` (`Fake::batteryMv`, fake definition, `boot()` reset, new cases)
- Modify: `sensor/src/main.cpp` (retained latch, `readBattery`, setup/loop/sleep hooks)
- Modify: `sensor/include/pins.h:18` comment → "Optional sensor interrupt; battery ADC through 100k/100k divider"

**Interfaces:**
- Consumes: `Engine::battery(uint16_t)`, `Engine::batteryLow`, `Battery::*` from Task 1.
- Produces (in `sensor/src/main.cpp` anonymous namespace):
  - `RTC_DATA_ATTR bool retainedBatteryLow = false;`
  - `uint64_t nextBatteryRead = 0;`
  - `inline uint16_t readBatteryMv();` — `SAMPLES` reads averaged, ×2; result `< MIN_VALID_MV` → `UINT16_MAX`; clamp above `UINT16_MAX - 1`.
  - `inline void sampleBattery(uint64_t now);` — `engine->battery(readBatteryMv())`, `nextBatteryRead = deadlineAfter(now, Battery::SAMPLE_INTERVAL_MS)`.

- [ ] **Step 1: Write the failing tests** in `tests/sensor_sleep_test.cpp`. Fake: `Fake::batteryMv` (per-pin reading in mV at the ADC, default `0`), `analogReadMilliVolts` returns it for `Pins::BATTERY_ADC`. `boot(..., clear=true)` also resets `retainedBatteryLow = false`. Cases:
  - `reportsBattery`: ADC 1955 → after boot + ticks, a sent telemetry frame decodes with `common.battery == 3910`, `BATTERY_UNAVAILABLE` clear.
  - `noCell`: ADC 0 → frames have `battery == UINT16_MAX`, flag set, no `BATTERY_LOW` sent.
  - `rereadsAfterInterval`: ADC changes 1955 → 1700 while awake and occupied; before `now + 60000` frames still carry 3910; after, 3400.
  - `timerWakeKeepsLatch`: ADC 1650 (3300 mV) cold boot → one `BATTERY_LOW` sent; reach deep sleep, `boot(ESP_SLEEP_WAKEUP_TIMER, false)` → no `BATTERY_LOW` on the wake.

- [ ] **Step 2: Run to see it fail**

Run: `uv run --locked python -m unittest tests.test_transport -k test_sensor_sleep -v`
Expected: FAIL — `analogReadMilliVolts` / `retainedBatteryLow` undefined.

- [ ] **Step 3: Implement** in `sensor/src/main.cpp`:
  - `setup()`: after `initializeEngine(...)`, on cold boot (`startColdBoot` path) clear `retainedBatteryLow`; then `engine->batteryLow = retainedBatteryLow;` and `sampleBattery(nowMs())`.
  - `loop()`: `if (now >= nextBatteryRead) sampleBattery(now);` before `engine->tick(...)`.
  - `sleepWhileVacant()`: `retainedBatteryLow = engine->batteryLow;` next to `retainedSeq`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --locked python -m unittest tests.test_transport -v`
Expected: all pass.

- [ ] **Step 5: Build firmware**

Run: `pio run -e sensor -e sensor-diagnostics -e sensor-window-test`
Expected: `SUCCESS`.

- [ ] **Step 6: Commit**

```bash
git add tests/fakes/Arduino.h tests/sensor_sleep_test.cpp sensor/src/main.cpp sensor/include/pins.h
git commit -m "feat: read the sensor battery through the ADC divider"
```

### Task 3: Docs

**Files:**
- Modify: `README.md:77` — drop "Battery measurement and"; keep the light-sleep deferral.
- Modify: `docs/PROTOCOL_SPEC.md:8` — battery no longer deferred; add one line: `BATTERY_LOW` sent once below 3400 mV, re-armed at 3600 mV.
- Modify: `docs/SETUP.md:145,223` — battery input used: 21700 cell → 100k/100k divider → GPIO0; readings null only with no cell (< 1000 mV).

- [ ] **Step 1: Edit the three files as above.**
- [ ] **Step 2: Verify** `rg -n -i 'battery.*deferred|battery input are currently unused|battery readings remain null'` returns nothing.
- [ ] **Step 3: Commit**

```bash
git add README.md docs/PROTOCOL_SPEC.md docs/SETUP.md
git commit -m "docs: describe battery measurement"
```

### Bench check (user, on hardware)

Flash the sensor, compare `battery_mv` with a multimeter across the cell (expect within ~50 mV), and confirm the value appears in `N/events` over MQTT.
