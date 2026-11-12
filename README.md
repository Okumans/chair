# Smart Chair

Smart Chair reports seat occupancy and raw MPU6050 movement to a computer or Raspberry Pi.
The computer stores reliable events in SQLite and owns logical sessions.

```text
ESP32-C3 chair → encrypted ESP-NOW → ESP32-S3 gateway → USB JSON → Python/SQLite
```

The chair samples movement at 25 Hz, persists reliable events before sending them, and
retries until the computer acknowledges durable storage. While occupied it stays awake;
while vacant it can deep sleep and wake from the seat switch or heartbeat timer.

## Start here

1. Follow [Setup and dependencies](docs/SETUP.md) to install the development tools.
2. [Read both boards' station MACs](docs/TRANSPORT.md#read-a-boards-mac-for-esp-now)
   and configure the shared, ignored pairing file.
3. Build and upload the normal sensor and gateway firmware using explicit serial ports.
4. Run the bridge with the gateway port, node mapping, and SQLite path:

   ```sh
   uv run tools/transport.py protocol \
     --port /dev/serial/by-id/YOUR_GATEWAY \
     --nodes ./nodes.json --db ./chair.sqlite
   ```

Only the bridge needs to own the gateway's serial port. No MQTT broker or background
service is required. Update the checked-in `nodes.json` to match your own sensor MAC.

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
| `shared/` | Wire codec, state engine, persistence and ESP-NOW/USB transport |
| `tools/` | Python bridge/console and MAC-only firmware |
| `tests/` | Python integration tests, native C++ tests and hardware fakes |
| `third_party/glaze/` | Vendored C++ JSON dependency |

## Documentation and limits

- [Setup and dependencies](docs/SETUP.md): installation, provisioning, builds, editor setup and checks.
- [Transport and Pi bridge](docs/TRANSPORT.md): MAC retrieval, commands, diagnostics and physical acceptance.
- [Protocol specification](docs/PROTOCOL_SPEC.md): message formats, state transitions and delivery rules.

Battery measurement, MQTT, systemd deployment, actual occupied light sleep and scheduled
radio windows remain deferred. Native tests do not establish physical radio reliability,
sample timing, power use or correct wiring; those require board testing.
