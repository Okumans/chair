# Smart Chair transport and Pi bridge

Chair → encrypted ESP-NOW v2 → ESP32-S3 gateway → UART JSON → Pi bridge (SQLite) → MQTT 5.
The backend owns logical sessions. Firmware reports physical state and raw MPU6050 movement.
Start with [Setup and dependencies](SETUP.md) for tool installation and development checks.
The wire contract and limitations are in [PROTOCOL_SPEC.md](PROTOCOL_SPEC.md).

## Read a board's MAC for ESP-NOW

Use the Wi-Fi station (STA) MAC for each board: the ESP-NOW transport uses
`WIFI_IF_STA`. The `mac-sensor` (ESP32-C3) and `mac-gateway` (ESP32-S3) environments
build only `tools/mac_address/main.cpp`, which reads this factory-derived address
and prints it once per second at 115200 baud. Pairing keys are not needed to run it.

Run these commands from the repository root. Stop the Python bridge and any other
serial monitor first. Connect the board's native USB port; on an S3 with separate
UART and native USB connectors, this firmware prints through the native USB connector.
Find each board's port with:

```sh
pio device list
```

**C3 chair sensor:** replace `YOUR_C3` with its actual serial-by-id device name.

```sh
make mac ENV=sensor PORT=/dev/serial/by-id/YOUR_C3
```

Record the printed address as the C3 sensor's STA MAC, then exit the monitor with Ctrl+C.

**S3 gateway:** replace `YOUR_S3` with its actual device name.

```sh
make mac ENV=gateway PORT=/dev/serial/by-id/YOUR_S3
```

Record this address separately, then exit with Ctrl+C. A current `/dev/ttyACM0` or
`/dev/ttyACM1` path also works, but port numbers can change when boards reconnect.
The `mac` target uploads the matching MAC-only environment and opens a monitor at
115200 baud on the same port. `PORT` is required, and `ENV` accepts `sensor` (the
default) or `gateway`. For example, `make mac ENV=gateway PORT=/dev/ttyACM1`.
The equivalent direct PlatformIO command for the S3 is
`pio run -e mac-gateway -t upload -t monitor --port /dev/ttyACM1`.

Put the recorded addresses in the ignored `shared/include/link_config.local.h`:

| Board | Firmware environment | Address setting |
|---|---|---|
| ESP32-C3 chair | `mac-sensor` | `LINK_SENSOR_MAC` |
| ESP32-S3 gateway | `mac-gateway` | `LINK_GATEWAY_MAC` |

Replace the placeholders below with the printed addresses. Preserve existing `LINK_PMK`
and `LINK_LMK` values; if the local file does not exist yet, copy
`shared/include/link_config.example.h` first and configure its keys as described below.
The same local file supplies the MACs and encryption keys to both firmware builds.

```cpp
#define LINK_SENSOR_MAC  "C3_STA_MAC_YOU_RECORDED"
#define LINK_GATEWAY_MAC "S3_STA_MAC_YOU_RECORDED"
```

Also set the chair's node ID and the **C3 sensor's MAC** in the Pi bridge configuration:

```toml
[bridge]
node_id = "chair-01"
sensor_mac = "C3_STA_MAC_YOU_RECORDED"
```

Installing the MAC utility replaces the board's application. Restore normal ESP-NOW
operation after recording and configuring both addresses:

```sh
pio run -e sensor -t upload --upload-port /dev/serial/by-id/YOUR_C3
pio run -e gateway -t upload --upload-port /dev/serial/by-id/YOUR_S3
```

The MAC utility does not write NVS, so stored chair configuration and events remain
intact. On the restored gateway, `hello.mac` on the Pi's gateway UART should match the S3 address and
`hello.peer_mac` the C3 address. Then run the Pi bridge as described below.

## Provision and build

The ignored `shared/include/link_config.local.h` contains station MACs and two independent
16-byte hex encryption keys. For a new pair, copy `link_config.example.h` there. Keep the
keys private. Both targets must use matching keys, channel and node ID. The shared node
ID is `LinkConfig::NODE_ID` in `shared/include/link_config.h`, default `chair-01`.

Seat: C3 GPIO5, contact to ground, internal pull-up. MPU6050: address 0x68, SDA6/SCL7.
INT21 is unused by the polling adapter. Battery measurement is disabled. Neither board
owns sessions. The gateway handles one configured encrypted chair peer.

```sh
make format
make lsp
pio run -e sensor -e gateway
make check
clangd --check=sensor/src/main.cpp --check-lines=1-1
clangd --check=shared/src/radio_transport.cpp --check-lines=1-1
clangd --check=gateway/src/main.cpp --check-lines=1-1
```

The clangd commands parse each complete translation unit; the line range limits editor
feature simulations. `make lsp` also supplies host compile commands for native tests.
Firmware uses C++23, pinned pioarduino 55.03.312-1 / Arduino-ESP32 3.3.12 and vendored
Glaze 9.1. No additional firmware dependency is needed for MPU6050 I2C reads.

Flash only when ready for physical acceptance, using explicit board ports:

```sh
pio run -e sensor -t upload --upload-port /dev/serial/by-id/YOUR_SENSOR
pio run -e gateway -t upload --upload-port /dev/serial/by-id/YOUR_GATEWAY
```

## Pre-commit checks

Run `make setup-hooks` once per clone. The plain Git shell hook checks staged formatting,
lint and tests without changing unstaged files. See [development checks](SETUP.md#development-checks)
for dependencies, commands and the separate firmware/LSP checks.

## Run the Pi bridge

The Pi runs the MQTT bridge service described in [MQTT_BRIDGE_SERVICE.md](MQTT_BRIDGE_SERVICE.md):

```sh
uv run python -m chair_mqtt.service --config ./bridge.toml
```

Its `[bridge]` table names the sensor and the durable SQLite path, and `[gateway]` the
serial port, `/dev/ttyAMA0` on a Pi 5 (see below). Use a local durable filesystem. Do not run two serial owners
or two bridge processes for the same gateway.

### Gateway UART wiring

The gateway application speaks JSON on UART1 at 115200 baud, not native USB. The pins
are `CHAIR_UART_TX_PIN` and `CHAIR_UART_RX_PIN` in `platformio.ini`. Native USB still
flashes it, and the `mac-gateway` tool still prints there.

| Pi | ESP32-S3 |
|---|---|
| GPIO14 TXD (pin 8) | GPIO18 RX |
| GPIO15 RXD (pin 10) | GPIO17 TX |
| GND (pin 6) | GND |

Both sides are 3.3 V. On a Pi 5, GPIO14/15 is UART0, `/dev/ttyAMA0`, and is off by
default: add `dtparam=uart0=on` to `/boot/firmware/config.txt` and reboot. There
`/dev/serial0` is the separate debug connector (`ttyAMA10`), not these pins. On a Pi 3, 4
or Zero W, use `/dev/serial0`: enable the serial hardware and disable the serial login
console (`raspi-config` → Interface Options → Serial Port), and add
`dtoverlay=disable-bt` to move it from the mini UART to the PL011. The port's user needs
the `dialout` group. The boot ROM and ESP-IDF logs stay on UART0 (GPIO43/44), so they
never reach the Pi. A UART cannot signal a gateway reboot, so unlike USB the bridge's
port stays open across one.

The bridge reconnects after serial loss. It discards partial input on reconnect. SQLite
uses WAL and synchronous FULL. Each reliable event commits before an exact EVENT_ACK
is queued. Retransmissions are stored once and ACKed again. A failed commit produces an
error without an ACK. Reliable events remain in SQLite and are published over MQTT;
movement is published best effort. READY is never stored
as an event or retained as availability across reconnect. The bridge waits for a matching
request-ID hello response after reconnect, and the gateway discards earlier queued READY
packets. Lease timing uses the Pi's monotonic clock. The current-state row tracks the chair's durable boot-generation counter and increasing
sequence numbers. Only STATE_SYNC establishes a previously unseen boot. Even an unseen
older boot cannot roll current state back. Generations are 16-character hex strings in
JSON and exact signed SQLite integers internally. A boot cannot change its generation
or reuse another boot's generation. The bridge rejects older databases, including those of the retired protocol-only
bridge, without modifying their contents; choose a new database or explicitly reset
disposable test data.

## Configuration

A backend submits a complete `ChairConfiguration` with `ChairClient.set_config`; see
[MQTT_LIBRARY.md](MQTT_LIBRARY.md). Every field is required. Heartbeat and movement
intervals accept 1..4294967295 seconds; zero is invalid. Values do not have to fit within
the heartbeat interval. Recommended movement interval: 1–3 s; a 75-sample buffer flushes
earlier regardless of the setting. Use a larger revision for changed settings. Revision
zero contains the firmware defaults. The radio fields are `radio_windows_enabled`,
`radio_interval_s`, `radio_open_s` and `radio_motion_tail_s`; the firmware defaults are
`true`, 30, 5 and 10. All values are validated even when windows are disabled; open time
cannot exceed interval.

Each command has a canonical nonzero UUID. Reusing a UUID with different content or
expiry is rejected. Commands wait for fresh occupied READY availability and
retry one at a time per node. A future timed READY bounds dispatch to its remaining
listen duration. Expired commands never dispatch again. A late reliable result remains
a durable fact in `events` and the command's `result`; an expired status stays expired.
The first result finalizes applied/rejected status. Chair deduplication retains up to
32 UUID records, evicts acknowledged records first, and protects the record and result
until its exact ACK is durable in the 32-entry outbox.

## Firmware operation

The deterministic engine stays separate from Arduino. `shared/include/chair_engine.h`
contains its documented API and state fields. The implementations share `SmartChair`:

| Source | Responsibility |
|---|---|
| `shared/src/chair_engine.cpp` | Operating states, seat debounce and motion batching |
| `shared/src/chair_delivery.cpp` | Reliable retries, ACKs, configuration and overflow |
| `shared/src/communication_window.cpp` | Radio window timing and shutdown |
| `shared/src/chair_storage.cpp` | Versioned durable snapshot encoding and validation |

`sensor/src/main.cpp` adapts seat input, persistence, radio and sleep;
`sensor/src/mpu6050.cpp` handles sensor registers. Public APIs document time units,
validation, ownership and failure behavior. Shared timings, bounds and thresholds live
in `shared/include/chair_constants.h`; role labels, USB names, diagnostics and radio
settings live in `shared/include/transport_constants.h`. Sensor NVS identifiers are in
`sensor/include/config.h`, and MPU6050 registers/settings stay local to its adapter.

The chair samples at 25 Hz, retaining signed raw acceleration and gyro values at ±2 g
and ±250 degrees/s. Classification compares acceleration magnitude to 1 g and raw gyro
axes to the initial thresholds; thresholds/reference need calibration on the installed
hardware. Start and stop debounce are 100 ms. Candidate and stop samples remain in
batches. Capacity or interval flushes produce bounded packets; stop/vacancy emits the
final boundary, even when empty. A buffer gap that exceeds the 16-bit sample offset
flushes early. No idle-only batches are emitted.

Reliable events and exact ACK removal use an atomic versioned NVS blob. Cold resets
create a new boot ID and atomically persist a larger generation with STATE_SYNC while
retaining old pending identities. Deep sleep retains the active boot and next sequence
in RTC memory, and keeps the same generation. V2 rejects earlier storage formats. Never erase NVS and reuse the
same node identity without explicit reprovisioning or reconciliation on the Pi; reused
generations are rejected. Counter exhaustion fails closed without wrapping. Ordinary overflow coalesces
history into current STATE_SYNC while retaining command results. A storage failure
keeps new transitions unsent and the chair awake until a current snapshot persists.
Corrupt stored data is reported and never silently overwritten with empty defaults.

Occupied sensing stays awake while communication follows the configured policy. Vacant
sleep preparation allows up to 2500 ms for pending work, cancels on reseating, and
rechecks the physical pin before radio stop. Wake is GPIO5 low or the configured
heartbeat timer. While occupied, the sensor defaults to a five-second radio window every
30 seconds and holds the radio through movement plus a ten-second tail. The gateway listens
continuously. The sensor keeps sampling while its radio is off; occupied light sleep remains
deferred. See [the radio-window specification](RADIO_WINDOWS_SPEC.md) for the v2 configuration
and failure semantics. Upgrade the bridge and both firmware images together.

## USB details and diagnostics

USB accepts UTF-8 JSON lines, LF/CRLF, maximum 8192 bytes excluding LF. Readers discard
oversized lines through newline. Firmware output has an eight-line RAM queue and resumes
short writes without interleaving messages. Overflow drops output; reliable radio retries
recover it. The radio input queue is also bounded. Best-effort movement may be lost.

The hardware USB CDC receive buffer is configured to hold a complete maximum-length
line plus LF before serial startup. Its driver otherwise defaults to 256 bytes and can
drop bytes during bursts. A transient false USB readiness indication preserves partial
input/output; only the hardware bus-reset event clears framing and starts a new USB
session. Native tests exercise 1,000 real bridge ACKs split across readiness changes,
maximum-size input, bus reset, partial output, and receive-buffer allocation failure.
The gateway build uses UART1 instead (`CHAIR_UART_TX_PIN`/`CHAIR_UART_RX_PIN`): the
same line limits and receive buffer apply, and there is no bus-reset event. The sensor
keeps hardware USB CDC.

An `invalid_json` error now includes `parse_error` (Glaze's error name), `byte_offset`
(where parsing stopped), and `line_bytes` (received line length). It does not echo raw
commands. These details distinguish malformed or truncated input without exposing its
contents.

Each message contains only fields relevant to its type; unrelated fields are omitted.
Unavailable battery voltage remains `battery_mv: null` in telemetry, and indefinitely
open READY messages retain `listen_remaining_ms: null`. Earlier SQLite facts containing
unrelated nulls remain compatible with compact retransmissions; do not reset the database.

Normal messages are typed `telemetry`, `event_ack`, and `command` envelopes. Boot IDs use
16 hex digits, command IDs canonical UUID strings. Unknown battery and indefinite listen
time are explicitly null. The gateway validates binary frames and the Pi validates the
MAC/node mapping. Corrupt or unauthorized frames never reach the state engine.

The bench console remains available for raw outbound diagnostic payloads:

```sh
uv run tools/transport.py /dev/ttyAMA0
```

Type text, `/hex 00ff010a`, or `/quit`. The input permits 1–1470 arbitrary bytes, but the
protocol gateway/chair now reject incoming bytes that are not valid provisioned protocol
frames. This console is useful for hello, malformed-input testing and manually encoded
protocol frames; arbitrary text is no longer forwarded as a normal received message.

Hello readiness is local initialization evidence. The existing 8.5 dBm transmit cap is
retained from bench bring-up. Match channel, peer MACs and key fingerprints on both
boards when diagnosing radio issues. This code does not claim physical delivery.

## Reset sensor state

Use this when the sensor's hello reports
`unsupported_or_corrupt_storage_explicit_v2_reset_required`. This procedure discards the sensor's persisted configuration, event outbox and command
records. Stop the bridge and serial monitors first; keep the pairing header and keys.
Choose a new empty SQLite database path, or remove the disposable database and its WAL/SHM
files while all database connections are closed. Do not reuse the old Pi history with a
reset sensor generation.

Flash `pio run -e reset-sensor -t upload --upload-port YOUR_SENSOR_PORT`, then open a
115200-baud monitor on that same port. Send the exact line `RESET-CHAIR-V2` and wait for
the success message. Disconnect power to clear the retained identity, reconnect and flash
the normal `sensor` firmware.
The reset utility only removes the chair application state key; it never runs at ordinary
startup. Verify a new STATE_SYNC, occupied READY, scheduled closure/reopening and a full
configuration round trip.

## Acceptance boundary

Native tests cover wire validation and sizes, motion timing and debounce, future radio
windows, retries, ACK loss/duplicates, overflow, reset persistence, command replay,
storage failure, SQLite commit-before-ACK, USB partial writes, and PTY serial reconnect.
Both firmware targets must build and clangd must report zero source errors.

Physical acceptance remains required for encrypted 1108-byte delivery, actual sample
timing, sensor calibration, configuration round trips, reset durability, seat/timer wake,
and power/radio measurements. Pi deployment of the MQTT bridge service has not been
performed; battery hardware and actual occupied light sleep are deferred. Validate the test schedule, radio restart, discovery after resets and
missed windows, movement loss reporting, and power consumption on the actual boards.

Before sending a command, the bridge challenges the sensor and bounds the lease from the
probe's send time. The full probe round trip therefore reduces the usable lease, including
radio and serial delays. Sensor, gateway and bridge must all support READY_PROBE and
READY_PROOF. Command deadlines are rechecked after database work and before each serial
write. If expiry interrupts a partial line, the bridge reconnects; it does not finish the
expired command. Bytes already submitted to the serial driver cannot be recalled.
Routine READY refreshes preserve pending proof responses; vacancy, policy changes and
availability expiry invalidate them.

A Wi-Fi SDK lifecycle call that never returns is not cancelled or overlapped with another
worker. Both nodes request an emergency reboot after 15 seconds outstanding; sensor reboot
waits for durable state, settled seat debounce and a final pin check. Eight permanently
missing callbacks also request a reboot rather than reuse a quarantined slot. Failed
storage postpones sensor reboot, and RAM movement can be lost across reset. HELLO uses a
cached radio snapshot so serial handling does not take a stuck worker's Wi-Fi locks.
See [the bench guide](RADIO_WINDOWS_BENCH.md) for reset dependencies, diagnostic firmware,
repeatable fault tests and remaining hardware acceptance work.
