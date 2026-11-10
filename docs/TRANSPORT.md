# ESP-NOW / USB transport

```text
Computer / Pi <-- USB --> S3 gateway <-- encrypted ESP-NOW --> C3 chair
```

The transport forwards arbitrary 1–250 byte payloads without interpreting them. Both
nodes expose the same USB interface. The gateway stays awake; the chair sleeps while
vacant and stays awake while occupied. It reports GPIO5 seat state, uptime, and
`wake_reason` after boot/wake, on debounced changes, and at the heartbeat interval.

## Chair deep sleep

After reporting vacancy, the chair waits for radio completion, then sleeps. GPIO5
LOW (seat contact closes) or the 900-second (15-minute) heartbeat timer wakes it. Each wake
reads/debounces the current contact and sends a fresh snapshot. Send completion waits
at most 1 second; delivery remains best effort without retries or persistence.

The SDK supplies the internal pull-up for LOW-level wake. Wake reliability and actual
current need physical verification. `HEARTBEAT_INTERVAL_MS` in `sensor/include/config.h`
uses the protocol specification default of 900 seconds.

The chair cannot receive radio/USB data while asleep. Its USB connection may disappear;
use the gateway console to observe wake reports. Downstream communication windows and
command queues remain outside scope. A local `sleep` message precedes sleep; forwarded
snapshots identify `wake_reason` as `reset`, `gpio`, or `timer`.

## Bring-up results — 11 November 2026

- Sensor MAC: `48:F6:EE:12:A0:D8`; gateway MAC: `44:1B:F6:8D:B7:9C`.
- Both nodes initialized on channel 1 with matching peer addresses and key fingerprints.
- Gateway → chair: `hello chair` arrived intact in the chair's `rx` output.
- Chair transmissions initially reported `mac_delivered:false`, including with modem sleep disabled.
- Capping the chair's transmit power at 8.5 dBm changed repeated results to `mac_delivered:true`, with the gateway unchanged.

The firmware now uses `esp_wifi_set_max_tx_power(34)` (quarter-dBm units). Lower power
may help supply/RF stability; the exact cause and usable range were not measured.

Both firmware targets built successfully. Four automated tests passed, covering
text/binary encoding, framing, invalid inputs, and a simulated USB exchange. Clangd
checks passed for shared sources.

## Setup

The existing boards use the ignored `shared/include/link_config.local.h`. For a new
pair, copy `link_config.example.h` there and configure both station MACs plus two
independent 16-byte hex keys (`LINK_PMK`, `LINK_LMK`). Both builds must use matching keys.
An unconfigured board's USB `hello` still reports its station MAC.

Build and flash each board using its current port:

```sh
pio run -e sensor -t upload --upload-port /dev/ttyACM_SENSOR
pio run -e gateway -t upload --upload-port /dev/ttyACM_GATEWAY
```

Use native USB and close other serial monitors before opening the console:

```sh
python tools/transport.py --list
python tools/transport.py /dev/serial/by-id/YOUR_DEVICE
```

If pyserial is missing, use `uv run tools/transport.py …`.

## Console and USB format

Type text to send UTF-8, `/hex 000aff0102` to send binary, or `/quit` to exit. Incoming
`rx` messages contain `data_hex` and, in the desktop console, `text` when valid UTF-8.
Received payloads are printed, not automatically echoed. Either node can be opened
with this console.

USB uses newline-delimited JSON: LF or CRLF, maximum 2048 bytes per line. Overlong
lines are discarded through the next newline. The radio carries the original bytes.

Computer → ESP:

```json
{"kind":"hello"}
{"kind":"tx","request_id":1,"data_hex":"68656c6c6f"}
```

ESP → computer:

```json
{"kind":"hello","role":"sensor","mac":"48:F6:EE:12:A0:D8","peer_mac":"44:1b:f6:8d:b7:9c","key_id":"9da721ec166c1019","channel":1,"power_save":0,"tx_power_dbm":8.50,"ready":true,"max_payload":250}
{"kind":"rx","source_mac":"44:1b:f6:8d:b7:9c","data_hex":"68656c6c6f"}
{"kind":"tx_result","request_id":1,"mac_delivered":true}
{"kind":"error","request_id":2,"reason":"radio_busy"}
```

`request_id` is 1–4294967295; zero identifies automatic chair reports. One radio send
may be in flight per node. `mac_delivered` confirms radio delivery, not application
processing or durable computer receipt. There are no retries or replay after USB
reconnection; queue overflow or disconnected USB can lose packets.

For troubleshooting, compare peer MACs, live channel, and `key_id` on both boards.
The key ID is the first 8 bytes of SHA-256(PMK bytes + LMK bytes). `power_save:0`
means modem sleep is disabled; `ready:true` confirms local initialization only.

## Scope

The planned application protocol is in [PROTOCOL_SPEC.md](PROTOCOL_SPEC.md). Event
ACKs, persistence, configuration commands, downstream communication windows, MQTT,
and a systemd service remain outside this transport implementation.

## Development checks

Five automated tests cover transport and sensor sleep sequencing using hardware stubs.
The sleep checks include debounce, send completion/timeout, late seat closure, and
wake-setup failure. Physical wake cycles and sleep current are not covered.

```sh
pio run -e sensor -e gateway
python -m unittest discover -s tests -v
make compiledb
clangd --check=shared/src/radio_transport.cpp --log=error
```
