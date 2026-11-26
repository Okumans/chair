# Backend and controller library

`ChairClient` attaches chair protocol handling to an application-owned Paho
MQTT 5 client. It does not create, connect, disconnect, or run that client.
The independent Pi bridge service is described in
[MQTT_BRIDGE_SERVICE.md](MQTT_BRIDGE_SERVICE.md).

Install uv and run `uv sync` from the repository root; uv provisions Python 3.11
or later. The package includes Paho, Pydantic and the other required
dependencies. Configure a reachable MQTT 5 broker, a running Pi bridge,
and a writable database directory before starting the backend application.
The `node_id`, `bridge_id` and `consumer_id` must match the Pi configuration;
the consumer ID identifies the backend whose receipts the Pi accepts.

```python
import asyncio
import sqlite3
import paho.mqtt.client as mqtt
from chair_mqtt import ChairClient, ClientConfig, SQLiteStorage

async def main():
    # Create application tables before the connector opens the database.
    db = sqlite3.connect("backend.sqlite")
    try:
        db.execute("CREATE TABLE IF NOT EXISTS app_events (boot TEXT, seq INTEGER)")
        db.commit()
    finally:
        db.close()

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
    try:
        client.connect("broker.example", 8883, **chair.connect_options())
        client.loop_start()
        while True:
            await asyncio.sleep(1)
            if chair.reconnect_required:
                # In a real service, abort the transport and reconnect the client.
                raise RuntimeError(f"MQTT recovery required: {chair.error}")
    finally:
        # The network loop must still run while the library unsubscribes.
        if await chair.detach():
            client.disconnect()
            client.loop_stop()
        else:
            raise RuntimeError("unsubscribe was not confirmed; abort and discard client")

asyncio.run(main())
```

Replace the broker address and CA path. Run the program until interrupted; the
bridge forwards sensor events and movement to the registered callbacks. Paho's
connection and network-loop calls in this small example are synchronous;
applications with strict event-loop latency requirements should run blocking
connection operations outside their event loop.

The application must create its own tables before receiving events. The callback
and all application database writes must use the transaction supplied by the
connector, with no independent commits or external side effects. Awaiting
another storage-backed `ChairClient` method, such as `set_config` or
`get_command`, inside the callback raises `RuntimeError` instead of waiting for
the callback's own transaction; start a task with `asyncio.create_task` so the
call runs after the callback returns.
An exception rolls back the fact, application writes, and pending receipt and
leaves the broker delivery unacknowledged. Duplicate facts do not invoke the
callback again. Movement is best effort and has no transaction or receipt.
Exceptions from the other handlers are logged and do not stop the client: the
delivery is still acknowledged and later handlers still run, because movement is
never redelivered and a command update is committed before its handlers run.

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

## Public client reference

All callbacks must be `async def` functions. Register one or more handlers with
the method, either as a decorator or by calling `chair.on_event(handler)`.
Handlers run sequentially on the event loop; do not block it or perform slow
synchronous I/O in them.

| Interface | Purpose and result |
| --- | --- |
| `ChairClient(mqtt_client, config, storage)` | Construct with a disconnected Paho MQTT 5 client, `ClientConfig`, and a `Storage` connector. |
| `await chair.attach()` | Initialize storage and install handlers before connecting; returns `chair`. Call on the event loop that will run callbacks. |
| `chair.connect_options()` | Return MQTT 5 options to unpack into `client.connect(..., **chair.connect_options())`. |
| `chair.on_event(handler)` | Call `handler(event, transaction)` for each new reliable fact. `event` is a `SensorEvent`; use the supplied transaction for application writes. |
| `chair.on_movement(handler)` | Call `handler(batch)` with a best-effort `MovementBatch`. |
| `chair.on_movement_loss(handler)` | Call `handler(loss)` with a `MovementLoss`; `loss.dropped_batches` is the sensor's reported loss count. |
| `chair.on_state(handler)` | Call `handler(state)` with a `SensorState`, the latest retained sensor fact. |
| `chair.on_bridge_status(handler)` | Call `handler(status)` with a `BridgeStatus`; inspect `status.status`, `status.reason`, and optional health fields. |
| `chair.on_command_update(handler)` | Call `handler(update)` with a `CommandUpdate` when a tracked command advances, or when a new reliable update arrives for another command. |
| `chair.on_command_error(handler)` | Call `handler(error)` with a `CommandError`, including its reason, retryability, and optional command ID. |
| `await chair.set_config(config, expires_in_seconds=...)` | Persist a complete `ChairConfiguration`; return a `CommandHandle` without waiting for the sensor. Expiry is 1–86,400 seconds. |
| `await chair.get_command(command_id)` | Return the latest `CommandState` from this connector, or `None` when that ID is unknown. |
| `chair.connected`, `chair.ready` | Connection status and whether all required subscriptions were granted QoS 1. |
| `chair.error`, `chair.reconnect_required` | Most recent worker error and whether the application must recover the MQTT connection. |
| `await chair.detach()` | Stop the worker and unsubscribe while the Paho network loop runs. Returns `True` when subscriptions were safely released, otherwise `False`. |
| `command.command_id` | UUID of a durably queued configuration; save it to look up the command after restart. |
| `await command.wait(timeout=None)` | Return `CommandState` on `applied`, `rejected`, `expired`, or `admission_unknown`. Sleeps until this client records a newer report or the command expires. Timeout raises `TimeoutError` and does not cancel the command; `detach()` ends the wait with `RuntimeError`. |

`ClientConfig` requires `node_id`, `bridge_id` and `consumer_id`; its optional
`allow_local_plaintext` defaults to `False`. `ChairConfiguration` requires all
eight fields shown in the command example. Durations are positive integer
seconds; the open duration cannot exceed the interval. Pydantic validation
rejects missing fields and incorrect types.

Sensor messages expose `node_id`, `boot_id`, `seq`, `uptime_ms`, `occupied`,
`battery_mv`, `config_revision`, and `health_flags`. `SensorEvent` adds bridge
and delivery metadata and puts the sensor message in `event.message`. A
`MovementBatch` preserves `batch_start_ms`, `duration_ms`, `end_reason`, and
each sample's `offset_ms`, `ax`, `ay`, `az`, `gx`, `gy`, and `gz` raw values.
`CommandState.status` can be `None`, `queued`, `delivered`, `applied`,
`rejected`, `expired`, or `admission_unknown`. Its `report` is `None` until a
report is received; `report.late_result` indicates a result after expiry.

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

To write a connector for another database, follow the step-by-step guide
and PostgreSQL example in [MQTT_STORAGE_CONNECTOR.md](MQTT_STORAGE_CONNECTOR.md).

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

Construct it with `SQLiteStorage("path/to/file.sqlite")`; the parent directory
must already exist, and `:memory:` is not supported. `ChairClient.attach()`
calls `initialize(...)`, and `detach()` closes the connector. Inside a SQLite
event callback, `await transaction.execute(sql, parameters=())` runs one SQL
statement in the fact's transaction. It returns selected rows for a query and
the affected-row count otherwise; transaction control statements are managed by
the library. The transaction is valid only while that callback is running.

The connector methods are public for custom integrations, although most
applications should let `ChairClient` call them:

| Async storage method | Input and output |
| --- | --- |
| `initialize(node_id, bridge_id, consumer_id)` | Open durable storage and bind the configured identities. |
| `ingest(topic, payload, *, qos, retain, on_fact)` | Commit a validated reliable delivery and receipt; return `True` for a new fact. Calls async `on_fact(route, value, transaction)` within that transaction. |
| `reject(topic, payload, reason)` | Persist a bounded diagnosis without recording a successful fact. |
| `pending_receipts()` / `confirm_receipt(publication_id)` | List pending receipt tuples / remove a receipt after successful broker PUBACK. |
| `enqueue_command(request, now_ms)` | Persist a `ConfigRequest`; return whether it was new. |
| `due_commands(now_ms)` | Return retry-ready command tuples and mark expired unadmitted commands unknown. |
| `command_submitted(command_id, now_ms)` | Schedule the next retry; return whether the command was updated. |
| `observe_report(topic, payload, *, qos, retain)` | Persist and deduplicate a report; return whether the latest state advanced. |
| `get_command(command_id)` | Return a dictionary containing `admitted`, `outcome`, `report_seq`, and `report`, or `None`. |
| `close()` | Close storage and its worker; the SQLite connector cannot be reused. |

`now_ms` and command expiry values are UTC Unix milliseconds. The connector
must not issue a receipt before its fact and application writes commit. The
library accepts any object implementing this contract; a custom connector
defines its own transaction object's methods. Its implementation must support
concurrent callers and the same deduplication and transactional guarantees.

The package also exports `BackendLedger`, `ControllerLedger`, `BridgeConfig`,
`BridgeHealth`, `CommandNotice`, and `ChairMqttBridge` for lower-level and
bridge-side integrations. Normal backend/controller applications should use
`ChairClient`; the Pi service uses `ChairMqttBridge`. The synchronous
`BackendLedger` exposes `ingest`, `reject`, `reject_digest`, `pending_receipts`,
and `confirmed` on a caller-owned SQLite connection. `ControllerLedger` exposes
`enqueue`, `due`, `submitted`, `observe`, and `get`; its caller must wire MQTT
subscriptions and retries. Their class and method docstrings describe the
transaction requirements. ESP data classes such as `StateSync`, `SeatEvent`,
`CommandResult`, `CommandReceipt`, `RadioReady`, and `ConfigSet` model the
validated protocol fields and are not connection managers.

For an application that deliberately embeds the Pi bridge, construct
`ChairMqttBridge(paho_client, BridgeConfig(...))` and call `attach()` before
connecting the Paho client. `connect_options()` returns its required connection
options; `start_gateway(port)` and `stop_gateway()` control the serial adapter.
`publish(message, source_mac=...)` accepts a sensor message that arrived over a
transport other than the gateway and returns a `PublishResult`: `status` is
`accepted`, `duplicate` or `ignored` (with a `reason`), and a reliable event,
including a retransmitted duplicate, carries the `EventAck` the caller must
deliver to the sensor. Data passed to `publish` is never acknowledged over the
gateway radio. `receive(timeout=None)` returns a command notice, not sensor data. `health()`
returns `BridgeHealth`, `retry_quarantined()` retries previously quarantined
publications after their cause is corrected, and `detach()` attempts an offline
publication and unsubscribe without disconnecting Paho. A `False` detach result
requires the owner to abort the transport and discard the client. Advanced
custom-serial integrations can use `gateway_opened()`, `gateway_received(envelope)`,
`gateway_next()`, `gateway_write(message, port)`, and `gateway_closed()`; the
adapter normally handles that sequence. These methods are synchronous and are
not needed by applications using the standalone service.

The application owns reconnecting its client. If `reconnect_required` is true,
abort the connection and reconnect to obtain redelivery; inspect `error` for the
cause. If detach returns false, keep manual acknowledgements enabled and abort
and discard that Paho client; after disconnect call `detach()` again to close
the storage worker. The library and tests do not establish Pi power-loss or
physical radio reliability.
