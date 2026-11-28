# Smart Chair

Smart Chair reports seat occupancy and raw MPU6050 movement to a computer or Raspberry Pi.
The Pi bridge stores reliable events in SQLite and publishes them over MQTT 5 to a backend,
which owns logical sessions.

```text
ESP32-C3 chair → encrypted ESP-NOW → ESP32-S3 gateway → UART JSON → Pi bridge (SQLite) → MQTT 5
```

The chair samples movement at 25 Hz, persists reliable events before sending them, and
retries until the computer acknowledges durable storage. While occupied it stays awake;
while vacant it can deep sleep and wake from the seat switch or heartbeat timer.

## Start here

1. Follow [Setup and dependencies](docs/SETUP.md) for Arch Linux, Debian or Ubuntu.
2. [Read both boards' station MACs](docs/TRANSPORT.md#read-a-boards-mac-for-esp-now)
   and configure the shared, ignored pairing file.
3. If the sensor reports unsupported or corrupt storage, follow the
   [sensor state reset](docs/TRANSPORT.md#reset-sensor-state). Build and upload the normal sensor and gateway firmware using explicit serial ports.
4. Copy `deploy/bridge.example.toml` to `bridge.toml`, set `sensor_mac` to your sensor's
   MAC, the gateway's serial port, a new `database_path` and the broker, then run the bridge:

   ```sh
   uv run python -m chair_mqtt.service --config ./bridge.toml
   ```

   To run it permanently on the Pi, install it as a systemd service with the
   [bridge service guide](docs/MQTT_BRIDGE_SERVICE.md#installation).

The bridge acknowledges and stores sensor facts even while the broker is unreachable, and
publishes them once it connects. For development against a local Mosquitto, set
`host = "localhost"`, `port = 1883` and `plaintext = true` under `[mqtt]` and remove
`ca_file`. A backend receives the data with `ChairClient`; see the
[library guide](docs/MQTT_LIBRARY.md) and the [bridge service](docs/MQTT_BRIDGE_SERVICE.md).
Only one program may own the gateway's serial port.

## Development

Run commands from the repository root:

```sh
make setup-hooks       # Once per clone: install the local Git pre-commit hook
make format            # Apply C/C++ and Python formatting
make check             # Check formatting, lint and Python/native C++ tests
make build ENV=sensor
make build ENV=gateway
make lsp               # Generate compile_commands.json for all environments and tests
```

The hook checks exactly the staged content and blocks failed checks. It is a plain Git
shell hook, without the Python pre-commit framework. Firmware builds and clangd checks
are separate; see [development checks](docs/SETUP.md#development-checks).

## Repository layout

| Path | Contents |
|---|---|
| `sensor/` | C3 hardware adapter, MPU6050 sampling and deep sleep |
| `gateway/` | S3 gateway entry point |
| `shared/` | Wire codec, state engine, persistence, ESP-NOW and USB/UART serial transport |
| `chair_mqtt/` | Python protocol bridge, serial bench console and MQTT library |
| `tools/` | Serial bench console, MQTT test console, radio-window scripts and MAC-only firmware |
| `tests/` | Python integration tests, native C++ tests and hardware fakes |
| `third_party/glaze/` | Vendored C++ JSON dependency |

## Documentation and limits

- [Setup and dependencies](docs/SETUP.md): installation, provisioning, builds, editor setup and checks.
- [Transport and Pi bridge](docs/TRANSPORT.md): MAC retrieval, commands, diagnostics and physical acceptance.
- [Protocol specification](docs/PROTOCOL_SPEC.md): message formats, state transitions and delivery rules.
- [MQTT library](docs/MQTT_LIBRARY.md): backend client, broker connection settings and usage.
- [MQTT bridge service](docs/MQTT_BRIDGE_SERVICE.md): Pi service configuration and deployment.
- The confirmed radio-window design covers wire v2, state-machine integration and implementation
  acceptance. [Radio windows specification](docs/RADIO_WINDOWS_SPEC.md)

- [Radio-window bench tools](docs/RADIO_WINDOWS_BENCH.md): recovery policy, contact traces, fault tests and current measurement.

Actual occupied light sleep remains deferred. The MQTT bridge
service and its systemd unit exist but have not yet been deployed or accepted on a Pi.
The occupied radio uses a test schedule; physical validation is still required.
Native tests do not establish physical radio reliability,
sample timing, power use or correct wiring; those require board testing.
