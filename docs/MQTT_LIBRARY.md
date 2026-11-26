# MQTT library

`chair_mqtt` provides the Pi-side facade for the contract in
[MQTT_SPECS.md](MQTT_SPECS.md). The application supplies an MQTT 5 Paho client;
the facade never creates it or calls its `connect`, `disconnect`, `loop_start`,
`loop_stop`, or reconnect methods. `attach()` installs chair-topic handlers,
enables manual acknowledgement, and configures the Last Will before connection.
The application continues to own broker credentials, TLS, the network loop, and
connection recovery.

The facade requires Paho callback API version 2 and must be attached **before**
the supplied client connects. It reserves the configured command and receipt
message callbacks. Manual acknowledgement affects the entire Paho client: if the
application uses other incoming subscriptions on the same client, it must
acknowledge those messages itself. Existing general callbacks are chained.

## Example integration

The following shows the boundary with an application that controls its own MQTT
client. The library assumes the Pi's UTC system clock is trustworthy.

```python
from pathlib import Path

import paho.mqtt.client as mqtt
from chair_mqtt import BridgeConfig, ChairMqttBridge

client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id="smartchair-pi-01",
    protocol=mqtt.MQTTv5,
)
client.tls_set(ca_certs="/path/to/broker-ca.pem")
# If needed, the application also configures client.username_pw_set(...).

bridge = ChairMqttBridge(
    client,
    BridgeConfig(
        node_id="chair-01",
        sensor_mac="02:00:00:00:00:01",
        bridge_id="pi-01",
        consumer_id="backend-01",
        database_path=Path("chair.sqlite"),
    ),
).attach()

# The application owns these operations and passes the facade's MQTT 5 options.
client.connect("broker.example", 8883, **bridge.connect_options())
client.loop_start()
bridge.start_gateway("/dev/serial/by-id/YOUR_GATEWAY")

# Application code may inspect bridge.health() and read bridge.receive(timeout=1).
# Command notifications are best effort; accepted commands are stored durably
# and dispatched by the gateway adapter even if notifications are not read.
```

For a local development broker only, set `allow_local_plaintext=True` in
`BridgeConfig` and connect to `127.0.0.1`, `::1`, or `localhost`. Network TLS must
verify both the broker certificate and hostname. The application must configure
its broker ACLs and credentials; a `consumer_id` field is not authentication.

`bridge.health()` exposes MQTT/subscription readiness, gateway status, storage,
pending and quarantined publications, and whether the application must reconnect
its client. `bridge.retry_quarantined()` explicitly retries the original work after
an operator fixes the cause. `bridge.receive()` reports accepted command requests;
it does not require the application to encode or dispatch ESP messages.

To stop, call `bridge.detach()` while the caller-owned network loop is still
running. It stops the optional serial adapter, publishes offline status and
unsubscribes the bridge's two topics. It returns `True` when the offline status
was acknowledged and the subscriptions were removed. If it returns `False`,
abort the transport without a graceful MQTT DISCONNECT so the broker can send
the Last Will, then call `detach()` again to finish stopping the worker if it is
still attached. If unsubscribe could not be confirmed, the library leaves manual
acknowledgements and its topic handlers in place to prevent silent data loss;
discard that client rather than reconnecting it. The application owns the network
loop and connection throughout.

## Data and testing

The MQTT store upgrades the bridge's SQLite schema and backfills existing reliable
events before use. Stop all owners and back up the database, including any WAL
files, before upgrading. The older protocol-only bridge refuses the newer schema;
returning to it requires an explicit compatible backup. Upgrade fails without
committing if existing data cannot be safely backfilled within the configured
outbox bounds.

Use `uv sync --locked` and `make check` for local validation. When Mosquitto is on
`PATH`, the broker integration test starts its own temporary loopback broker; it
does not contact a production broker. A real backend is required to issue durable
`MQTT_ACK` receipts. `BackendLedger` and `ControllerLedger` provide durable
building blocks for application-owned backend and controller connections; their
callers must wire the respective subscriptions, acknowledgements and retry loop.

The library and tests do not establish hardware radio reliability, correct sensor
wiring, or Pi power-loss behavior. Those require separate on-device checks.
