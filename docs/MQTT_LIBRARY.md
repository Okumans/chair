# Backend and controller library

`ChairClient` attaches chair protocol handling to an application-owned Paho
MQTT 5 client. It does not create, connect, disconnect, or run that client.
The independent Pi bridge service is described in
[MQTT_BRIDGE_SERVICE.md](MQTT_BRIDGE_SERVICE.md).

```python
import asyncio
import paho.mqtt.client as mqtt
from chair_mqtt import ChairClient, ClientConfig, SQLiteStorage

async def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id="chair-backend", protocol=mqtt.MQTTv5)
    client.tls_set(ca_certs="/path/to/broker-ca.pem")
    chair = ChairClient(client, ClientConfig(node_id="chair-01", bridge_id="pi-01",
                                           consumer_id="backend-01"),
                        SQLiteStorage("backend.sqlite"))

    @chair.on_event
    async def event_received(event, transaction):
        await transaction.execute(
            "INSERT INTO app_events (boot, seq) VALUES (?, ?)",
            (event.message.boot_id, event.message.seq),
        )

    @chair.on_movement
    async def movement_received(batch):
        for sample in batch.samples:
            print(sample.ax, sample.ay, sample.az, sample.gx, sample.gy, sample.gz)

    @chair.on_command_update
    async def command_updated(update):
        print(update.command_id, update.status)

    await chair.attach()  # Attach before connecting, on the application event loop.
    client.connect("broker.example", 8883, **chair.connect_options())
    client.loop_start()
    # Run application work here. Monitor chair.error and chair.reconnect_required.
    # On reconnect_required, abort and reconnect the caller-owned client.
    # Keep the loop running while detaching.
    if await chair.detach():
        client.disconnect()
        client.loop_stop()
    else:
        raise RuntimeError("unsubscribe was not confirmed; abort and discard client")

asyncio.run(main())
```

The application must create its own tables before receiving events. The callback
and all application database writes must use the transaction supplied by the
connector, with no independent commits or external side effects. It must not
call other `ChairClient` storage methods while its transaction callback runs.
An exception rolls back the fact, application writes, and pending receipt and
leaves the broker delivery unacknowledged. Duplicate facts do not invoke the
callback again. Movement is best effort and has no transaction or receipt.

Additional async decorators are `on_state`, `on_movement_loss`,
`on_bridge_status`, and `on_command_error`. On the supplied client, configure
verified TLS and credentials before connection. For local testing only,
`ClientConfig(allow_local_plaintext=True, ...)` permits loopback plaintext.
Manual acknowledgements affect the whole Paho client, so the application must
acknowledge any unrelated subscriptions itself. Existing general callbacks are
chained; chair topic callbacks belong exclusively to this library.
Command-update callbacks are notifications after the local report state commits;
they are not a durable work queue. After a restart or callback error, use
`get_command` to recover the latest state for commands submitted by this client.
The library stores reliable report facts as well as sensor facts in its backend
ledger, but does not expose a separate report-history query in this version.

## Commands

```python
from chair_mqtt import ChairConfiguration

command = await chair.set_config(
    ChairConfiguration(revision=2, heartbeat_s=900, movement_batch_s=3,
                       movement_enabled=True, radio_windows_enabled=True,
                       radio_interval_s=30, radio_open_s=5, radio_motion_tail_s=10),
    expires_in_seconds=300,
)
print(command.command_id)  # The request is durably queued at this point.
result = await command.wait(timeout=60)  # Optional; timeout does not cancel it.
print(result.status)
# Also available: await chair.get_command(command.command_id)
```

The client waits for its QoS-1 subscriptions before submitting, persists the
same command bytes for retries, and tracks updates by report sequence. A local
`admission_unknown` outcome means expiry occurred without admission confirmation;
a later report can update it. Applications that need late reports should keep
the client running and handle `on_command_update` after an initial wait returns.

## Storage connector contract

The async `Storage` protocol in `chair_mqtt.async_storage` specifies the methods
custom connectors implement. `initialize` binds node, bridge and consumer IDs.
`ingest` validates and deduplicates reliable events and report updates, invokes
`on_fact(route, value, transaction)` exactly once for a new fact in the same
transaction, and durably enqueues its backend receipt before commit. `reject`
durably records a bounded diagnosis. `pending_receipts` returns
`(publication_id, topic, payload)` tuples; `confirm_receipt` removes one only
after successful PUBACK.

`enqueue_command` stores a `ConfigRequest` and its exact bytes before sending;
`due_commands` returns `(command_id, topic, payload, expires_ms)` tuples and marks
expired unadmitted commands unknown; `command_submitted` schedules retry.
`observe_report` atomically checks identity and sequence and returns whether the
latest status advanced; `get_command` returns `None` or a dictionary with
`admitted`, `outcome`, `report_seq`, and `report`. `close` releases resources.
All methods are async and may be called sequentially by the client worker or
concurrently by application tasks, so implementations must serialize their own
transactions. Delivery callbacks are not a guarantee of exactly-once external
side effects. `SQLiteStorage` is the default example; it uses a dedicated worker.

The application owns reconnecting its client. If `reconnect_required` is true,
abort the connection and reconnect to obtain redelivery; inspect `error` for the
cause. If detach returns false, keep manual acknowledgements enabled and abort
and discard that Paho client; after disconnect call `detach()` again to close
the storage worker. The library and tests do not establish Pi power-loss or
physical radio reliability.
