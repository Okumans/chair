# Smart Chair transport and Pi bridge

Chair → encrypted ESP-NOW v2 → ESP32-S3 gateway → USB JSON → SQLite/Pi application.
The Pi owns logical sessions. Firmware reports physical state and raw MPU6050 movement.
The wire contract and limitations are in [PROTOCOL_SPEC.md](PROTOCOL_SPEC.md).

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
python -m unittest discover -s tests -v
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

## Run the Pi bridge

The Python tool needs pyserial 3.5. `uv run tools/transport.py ...` installs the script's
pinned dependency in an isolated environment, or use an existing Python with pyserial.
Do not run two serial owners or two bridge processes for the same gateway.

Create an explicit node mapping JSON file, using the sensor station MAC from USB hello:

```json
{"chair-01":"02:00:00:00:00:01"}
```

Use the actual MAC, not the example address. The serial path, mapping file and SQLite
path are mandatory. Prefer a stable by-id serial path and a local durable filesystem.

```sh
python tools/transport.py --list
python tools/transport.py protocol \
  --port /dev/serial/by-id/YOUR_GATEWAY \
  --nodes ./nodes.json --db ./chair.sqlite
```

The bridge reconnects after serial loss. It discards partial input on reconnect. SQLite
uses WAL and synchronous FULL. Each reliable event commits before an exact EVENT_ACK
is queued. Retransmissions are stored once and ACKed again. A failed commit produces an
error without an ACK. JSON output is structured for a future MQTT adapter.

Bridge output includes `accepted` (with duplicate flag and reception time), `hello`,
`tx_result`, `error` and `disconnected`. `mac_delivered` reports MAC delivery only.
Reliable events remain in SQLite; movement and READY are streamed. READY is never stored
as an event or retained as availability across reconnect. The current-state row tracks the chair's durable boot-generation counter and increasing
sequence numbers. Only STATE_SYNC establishes a previously unseen boot. Even an unseen
older boot cannot roll current state back. Generations are 16-character hex strings in
JSON and exact signed SQLite integers internally. A boot cannot change its generation
or reuse another boot's generation. Legacy snapshots without the counter are stored as
facts and ACKed, but cannot establish current state. Upgrading old Pi databases clears
only derived ordering rows; durable events and queued commands remain intact.

## Submit and inspect configuration

Configuration is a complete transaction. Both intervals accept 1..4294967295 seconds;
zero is invalid. Values do not have to fit within the heartbeat interval. Recommended
movement interval: 1–3 s; a 75-sample buffer flushes earlier regardless of the setting.
Use a larger revision for changed settings. Revision zero contains the firmware defaults.

The configuration command runs independently of the serial-owning bridge and adds a
row to the same SQLite database. `--expires-at` is UTC Unix time in seconds. This example
expires one hour after submission:

```sh
python tools/transport.py config --nodes ./nodes.json --db ./chair.sqlite \
  --node chair-01 --revision 1 --heartbeat-s 900 --movement-batch-s 3 \
  --movement-enabled true --expires-at "$(date -d '+1 hour' +%s)"
python tools/transport.py commands --nodes ./nodes.json --db ./chair.sqlite
```

An optional `--command-id` accepts a canonical nonzero UUID. Reusing a UUID with different
content or expiry is rejected. Commands wait for fresh occupied READY availability and
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
in RTC memory, and keeps the same generation. The NVS decoder upgrades the earlier
counter-free blob format without dropping pending events. Never erase NVS and reuse the
same node identity without explicit reprovisioning or reconciliation on the Pi; reused
generations are rejected. Counter exhaustion fails closed without wrapping. Ordinary overflow coalesces
history into current STATE_SYNC while retaining command results. A storage failure
keeps new transitions unsent and the chair awake until a current snapshot persists.
Corrupt stored data is reported and never silently overwritten with empty defaults.

Awake occupied operation keeps communication open and bypasses occupied sleep. Vacant
sleep preparation allows up to 2500 ms for pending work, cancels on reseating, and
rechecks the physical pin before radio stop. Wake is GPIO5 low or the configured
heartbeat timer. Communication windows and occupied sleep are represented/tested in the
engine, but no hardware schedule or downstream window configuration is enabled.

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

An `invalid_json` error now includes `parse_error` (Glaze's error name), `byte_offset`
(where parsing stopped), and `line_bytes` (received line length). It does not echo raw
commands. These details distinguish malformed or truncated input without exposing its
contents. No MQTT broker or background service is needed for this serial/SQLite bridge.

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
python tools/transport.py /dev/serial/by-id/YOUR_GATEWAY
```

Type text, `/hex 00ff010a`, or `/quit`. The input permits 1–1470 arbitrary bytes, but the
protocol gateway/chair now reject incoming bytes that are not valid provisioned protocol
frames. This console is useful for hello, malformed-input testing and manually encoded
protocol frames; arbitrary text is no longer forwarded as a normal received message.

Hello readiness is local initialization evidence. The existing 8.5 dBm transmit cap is
retained from bench bring-up. Match channel, peer MACs and key fingerprints on both
boards when diagnosing radio issues. This code does not claim physical delivery.

## Acceptance boundary

Native tests cover wire validation and sizes, motion timing and debounce, future radio
windows, retries, ACK loss/duplicates, overflow, reset persistence, command replay,
storage failure, SQLite commit-before-ACK, USB partial writes, and PTY serial reconnect.
Both firmware targets must build and clangd must report zero source errors.

Physical acceptance remains required for encrypted 1108-byte delivery, actual sample
timing, sensor calibration, configuration round trips, reset durability, seat/timer wake,
and power/radio measurements. MQTT, systemd, battery hardware, actual occupied light
sleep and hardware communication schedules are deferred.
