# Pi bridge service

The standalone bridge reads the gateway over serial and publishes to an MQTT 5
broker. It reads a TOML file such as `deploy/bridge.example.toml` and runs under
the systemd unit `deploy/chair-mqtt-bridge.service`.

## Installation

Run these steps on the Pi. Adjust the account, paths and serial device to suit
your installation.

### 1. Get the code

Installing needs a checkout that contains the `chair_mqtt` package:

```sh
git clone https://github.com/Okumans/chair.git ~/chair
```

### 2. Create the service account

```sh
sudo useradd --system --no-create-home -U chair
id chair
```

`-U` creates the `chair` group that the unit's `Group=` names. Serial access
comes from the unit's `SupplementaryGroups=dialout`, so the account itself
needs no extra groups.

### 3. Install the package

The repository root is the `chair-mqtt` Python package. pip installs only
`chair_mqtt/` and its dependencies, not the firmware.

```sh
sudo python3 -m venv /opt/chair-mqtt
sudo /opt/chair-mqtt/bin/pip install ~/chair
/opt/chair-mqtt/bin/python -c "import chair_mqtt.service; print('ok')"
```

### 4. Enable the Pi UART

The gateway connects to GPIO14/15. In `sudo raspi-config`, open Interface
Options → Serial Port, answer **No** to the login shell and **Yes** to the
serial hardware, then reboot. The device is `/dev/ttyAMA0` on a Pi 5 and
`/dev/serial0` on a Pi 3 or 4.

### 5. Write the configuration

```sh
sudo mkdir -p /etc/chair-mqtt
sudo cp ~/chair/deploy/bridge.example.toml /etc/chair-mqtt/bridge.toml
sudo nano /etc/chair-mqtt/bridge.toml
```

- `[bridge]`:
  - `node_id` and `sensor_mac` identify the provisioned sensor.
  - `bridge_id` and `consumer_id` must match the backend's `ChairClient` configuration.
  - `database_path` stays under `/var/lib/chair-mqtt`, which the unit creates.
- `[mqtt]`: the keys of `BrokerConfig`: `host`, `port` (default 8883), `client_id`, an optional `ca_file` and `plaintext`.
  - For a TLS broker, copy its CA certificate to `/etc/chair-mqtt/` and set `ca_file`.
  - Without `ca_file`, the system trust store is used.
- `[gateway]`: `port` is the UART device from step 4.

### 6. Broker credentials

If the broker requires a login, create `/etc/chair-mqtt/credentials`. Skip this
step if the broker accepts anonymous clients.

```sh
CHAIR_MQTT_USERNAME=...
CHAIR_MQTT_PASSWORD=...
```

```sh
sudo chmod 600 /etc/chair-mqtt/credentials
```

### 7. Install the unit

```sh
sudo cp ~/chair/deploy/chair-mqtt-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
```

If the broker runs on the same machine as its own systemd service, order the
bridge after it. This is optional, because the bridge retries until the broker
is reachable. Open a drop-in with `sudo systemctl edit chair-mqtt-bridge` and
add, using your broker's unit name:

```ini
[Unit]
Wants=<broker>.service
After=<broker>.service
```

### 8. Start and verify

```sh
sudo systemctl enable --now chair-mqtt-bridge
journalctl -u chair-mqtt-bridge -f
```

Subscribe to the bridge's status and event topics with any MQTT client to
confirm that it publishes.

### Updating

The installation is a copy, not a link to the checkout. Reinstall after pulling,
then restart the service:

```sh
git -C ~/chair pull
sudo /opt/chair-mqtt/bin/pip install ~/chair
sudo systemctl restart chair-mqtt-bridge
```

## Broker security

The bridge trusts every `B/receipts` message from its configured consumer. It
also admits every well-formed command on `N/commands/config`. Anyone who can
publish to those topics can therefore mark events as delivered, which stops
their retransmission, or reconfigure the chair. Do not expose an anonymous
broker beyond a trusted network:

- Either bind the listener to the local host only, or require authentication.
- Use an ACL so that only the designated backend account can publish to
  `B/receipts` and the command topics.

## Configuration reference

The configuration contains no credentials. The service refuses a `username` or
`password` in the `[mqtt]` table. It reads `CHAIR_MQTT_USERNAME` and
`CHAIR_MQTT_PASSWORD` only from the environment, which systemd's
`EnvironmentFile` loads from `/etc/chair-mqtt/credentials`.

TLS always verifies the broker certificate and host name before anything is
sent, against `ca_file` or else the system trust store. `plaintext = true`
disables TLS and is meant only for development against a broker on the same
machine. It is rejected for any host other than `localhost`, `127.0.0.1` or
`::1`.

## Runtime behaviour

The service opens the gateway serial port immediately and connects to the broker in
the background, so sensor facts are acknowledged and stored durably while the
broker or network is unavailable; they are published once it is reachable.
The bridge reconnects with a 1–30 second exponential backoff (without the
jitter the MQTT specification asks for), and when it needs MQTT
redelivery it drops the connection itself, at most once every 30 seconds.
Systemd restarts the service only after a fatal protocol error or a storage
failure, which a reconnect cannot fix. On stop the service publishes an offline
status and disconnects once the broker acknowledges it, or aborts the
connection so the broker publishes the Will. A restarted service starts a clean
broker session and republishes its durable publications. A database holding data
from the retired protocol-only bridge (`transport.py protocol`) is refused at
startup without being changed; point `database_path` at a new file. Deployment and hardware acceptance
have not yet been performed.
