# Setup and dependencies

Run these commands from the repository root. The development and bridge test workflow
targets Linux; serial reconnect tests use pseudo terminals. A computer/Pi running only
the bridge does not need the C++ tools or PlatformIO.

## Dependencies

| Tool | Used for |
|---|---|
| Git and Make | Checkout, development targets and the local commit hook |
| Python 3 and uv | Bridge, test runner and isolated Python dependencies |
| PlatformIO Core (`pio`) | Downloading ESP toolchains, building, uploading and monitoring |
| Host C++23 compiler (`c++`) | Native tests; separate from the ESP cross-compilers |
| clang-format | C/C++ formatting and formatting checks |
| Ruff (`ruff`) | Python formatting and lint |
| clangd | Optional editor diagnostics using the generated compilation database |

The firmware platform is pinned in `platformio.ini` to pioarduino `55.03.312-1`.
Glaze is vendored in `third_party/glaze`; no separate download is needed. MPU6050 access
uses Arduino Wire directly. Python's SQLite support is in the standard library; no
SQLite server is needed. The package requires `pyserial>=3.5,<4`; the standalone
`tools/` scripts pin `pyserial==3.5`.

The host compiler must support the C++23 features used by Glaze as well as `-std=c++23`.
`make test` verifies this by compiling and running the native tests. Formatter and lint
versions are currently taken from your PATH, rather than pinned by the repository.

### Arch Linux development machine

Install the system tools, then install PlatformIO in an isolated uv tool environment:

```sh
sudo pacman -S --needed git make gcc python uv clang ruff
uv tool install platformio
uv tool update-shell
```

Start a new shell if needed so `pio` is on PATH. If PlatformIO is already installed,
use that installation instead of creating another one.

### Debian and Ubuntu development machine

Use Debian 13 (Trixie) or Ubuntu 24.04 LTS or newer as the starting point for full
development. Older releases can run the bridge, but their default compiler or formatter
may be too old for the native tests and `.clang-format` configuration.

```sh
sudo apt update
sudo apt install git build-essential g++-14 python3 python3-venv \
  curl ca-certificates clang-format clangd
```

On Ubuntu, enable the Universe repository if these packages are unavailable.
[Ubuntu 24.04 provides `g++-14`](https://packages.ubuntu.com/noble/g%2B%2B-14);
[Debian 13 defaults to GCC 14](https://packages.debian.org/trixie/g%2B%2B).
Check `c++ --version`: native tests invoke `c++` directly, so installing `g++-14`
alone does not select it on Ubuntu. If `c++` still selects an older GCC, these commands
register and select GCC 14 as the **system-wide** C++ compiler alternative:

```sh
sudo update-alternatives --install /usr/bin/c++ c++ /usr/bin/g++-14 140
sudo update-alternatives --set c++ /usr/bin/g++-14
c++ --version
```

Install uv using its [official standalone installer](https://docs.astral.sh/uv/getting-started/installation/),
then install the Python tools in isolated environments:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
. "$HOME/.local/bin/env"
uv tool install platformio
uv tool install ruff
uv tool update-shell
```

Open a new shell if necessary for the tool PATH changes. Do not use `sudo pip` or
`--break-system-packages`; uv keeps these tools outside the distro-managed Python.

For USB serial access, inspect `ls -l /dev/ttyACM*`. If the device belongs to `dialout`,
add your user to that group, then log out and back in and reconnect the board:

```sh
sudo usermod -aG dialout "$USER"
```

If upload access still fails, follow PlatformIO's
[udev rules instructions](https://docs.platformio.org/en/latest/core/installation/udev-rules.html).
Keep uploads and the bridge running as your normal user.

### Verify the development tools

Confirm the commands are available:

```sh
python3 --version
uv --version
pio --version
c++ --version
clang-format --version
ruff --version
clangd --version
```

Initial builds download the pinned ESP platform, SDK and cross-compilers. The first uv
test or bridge invocation also downloads Python dependencies. Allow network access and
writable PlatformIO/uv caches during this first setup.

### Bridge-only computer or Raspberry Pi

On Debian, Ubuntu or Raspberry Pi OS, install `python3`, `python3-venv`, `curl` and
`ca-certificates` with apt, then install uv using the standalone installer above.
The compiler, formatter, Ruff, clangd and PlatformIO are unnecessary for bridge-only use.

Run `uv sync` from the repository root to install the bridge with its MQTT dependencies
(Paho, Pydantic). The bench console alone needs only pyserial: `uv run tools/transport.py
--list` lists serial devices, or install `pyserial==3.5` in your own virtual environment
and use `python tools/transport.py ...`. Do not install packages into the system Python
just to run the bridge.

## Provision the boards

1. Connect each board with a data-capable USB cable. Use native USB on the S3.
2. Follow [MAC retrieval](TRANSPORT.md#read-a-boards-mac-for-esp-now) for the C3 and S3.
   `make mac ENV=sensor PORT=...` and `make mac ENV=gateway PORT=...` replace the
   application with MAC-only firmware; restore the normal firmware afterward.
3. Create the local pairing header once:

   ```sh
   cp shared/include/link_config.example.h shared/include/link_config.local.h
   ```

   Edit the local file with both STA MACs and two independent random 16-byte keys,
   encoded as 32 hex characters each. For example, run
   `python3 -c 'import secrets; print(secrets.token_hex(16))'` separately for each key.
   Both builds use this same header. Keep it private; Git ignores it. Preserve an
   existing local file instead of overwriting its pairing keys.
4. Set `sensor_mac` in the bridge configuration (see below) to your **C3 sensor's** STA
   MAC. The tracked `nodes.json`, used only by the radio-window bench tool, maps `chair-01`
   for the development pair and is not a universal default.

The seat contact connects GPIO5 to ground with an internal pull-up. The MPU6050 uses
I2C address `0x68`, SDA GPIO6 and SCL GPIO7. Confirm wiring against
`sensor/include/pins.h`; the optional interrupt is unused.

The battery is one 21700 Li-ion cell measured through a 100k/100k divider into GPIO0, so
`battery_mv` is twice the ADC reading (8 samples averaged, read at every wake and every 60 s
while awake). Below 1000 mV the cell counts as absent and `battery_mv` is null. The chair
sends `BATTERY_LOW` when the cell drops below 3400 mV, keeps it queued until the Pi
acknowledges it, and re-arms at 3600 mV. The
divider draws about 20 µA continuously, including in deep sleep.

## Build, upload and run

```sh
make build ENV=sensor
make build ENV=gateway
pio device list
pio run -e sensor -t upload --upload-port /dev/serial/by-id/YOUR_C3
pio run -e gateway -t upload --upload-port /dev/serial/by-id/YOUR_S3
uv run python -m chair_mqtt.service --config ./bridge.toml
```

Create `bridge.toml` from `deploy/bridge.example.toml`; [the bridge service
guide](MQTT_BRIDGE_SERVICE.md) describes its keys and a local plaintext broker.

Replace serial placeholders with actual device paths. The commands above pass
`--upload-port` directly to PlatformIO; `make upload`, `make flash`, `make monitor`
and `make mac` accept the same device as `PORT=...`
(for example `make upload ENV=sensor PORT=/dev/serial/by-id/YOUR_C3`). The firmware
can build without pairing values, but the normal radio link will not become ready
until valid matching settings are supplied.

Use a local durable filesystem for SQLite and keep database files out of commits.
Stop the bridge before opening a monitor on the gateway's UART. Only one program
should read/write a serial port at a time; uploading over native USB does not use the
bridge's UART. On permission errors, inspect
`ls -l /dev/ttyACM*` and your distribution's serial-device group or udev rules; reconnect
and log in again after changing permissions. Run the bridge as your normal user.

A backend sends configuration commands with `ChairClient.set_config`; see the
[library guide](MQTT_LIBRARY.md) and the [configuration rules](TRANSPORT.md#configuration).

## Development checks

```sh
make setup-hooks
make format
make check
make lsp
```

`make check` runs `format-check`, `lint` and `test`. This covers C/C++ and Python
formatting, shell syntax for the hook, Ruff lint, and all Python/native C++ tests.
Native builds treat compiler warnings as errors. Individual targets are available when
diagnosing a failure. `make format` changes files; stage the resulting edits yourself.

`make setup-hooks` sets this checkout's local `core.hooksPath` to `.githooks`, replacing
any previous local hook-path setting. The plain Git shell hook checks a temporary copy
of exactly the staged files; unstaged edits and untracked files remain untouched.
Failures block the commit. Fix the error, stage the corrected files and retry. Install
the hook separately in each clone. The Python pre-commit framework is not required.

The hook does **not** build ESP firmware or run clangd. Before committing firmware
changes, also build both targets and check the affected translation units:

```sh
pio run -e sensor -e gateway
make lsp
clangd --check=sensor/src/main.cpp
clangd --check=shared/src/radio_transport.cpp
clangd --check=gateway/src/main.cpp
```

`make lsp` regenerates the ignored `compile_commands.json` for both normal firmware
targets, both MAC utilities and native tests. It does not itself run diagnostics.
The checked-in `.clangd` contains absolute `/home/okumnas/.platformio` SDK paths;
adjust those paths for your home directory and installed toolchain versions on another
machine. Clang has no Xtensa backend, so gateway diagnostics use a header-parsing
workaround; the PlatformIO build remains the firmware compilation check.

## Troubleshooting

- **Missing `pio`, `ruff` or formatter:** verify PATH in the terminal/editor that starts
  Git. Hooks inherit that environment and need the same commands as `make check`.
- **`ready: false`:** check the local pairing header, both MACs, keys and channel.
- **`invalid_json`:** stop extra serial owners and inspect `parse_error`, `byte_offset`
  and `line_bytes`; see [serial diagnostics](TRANSPORT.md#usb-details-and-diagnostics).
- **No movement:** check MPU6050 wiring and health flags.
- **Battery null:** no cell is connected or GPIO0 reads below 500 mV; check the divider.
- **No serial output:** check the USB cable, native USB connector and device path after
  reconnecting. Prefer `/dev/serial/by-id/` when available.

Software checks do not replace the [physical acceptance tests](TRANSPORT.md#acceptance-boundary).
