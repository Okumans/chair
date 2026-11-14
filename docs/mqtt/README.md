# Executable MQTT contract and backend reference

This kit accompanies [MQTT_SPECS.md](../MQTT_SPECS.md). It is a reference artifact,
not the production Pi forwarding service. It never opens a serial port or runs
ESP commands, and no example is automatically installed or started as a service.
On the Pi this directory lives beside `MQTT_SPECS.md` under `Desktop/Project/mqtt`.

## Files

- `mqtt.schema.json`: closed JSON Schema draft 2020-12 definitions for every public
  payload family and exported ESP packet type. All `$ref` targets are local.
- `contract.py`: strict JSON/topic/QoS/retain validation and cross-field checks.
- `fixtures.json`: 69 named positive/negative wire-contract examples.
- `backend_receipt_example.py`: durable reference backend with a separate SQLite
  database, semantic deduplication and a transactional outgoing receipt queue.
- `tests/`: contract/SQLite/fault tests plus a real-broker test when Mosquitto exists.
- `requirements.txt`: tested top-level dependency pins; install in a virtual environment.
- `VALIDATION.md`: completed checks and their implementation/physical limits.

Schema validation alone cannot check all semantics: JSON's exact integer syntax,
configuration opening versus interval, topic/body identity, strict sample offsets,
result/report consistency and configured ownership require `contract.validate()`
or equivalent checks. Schema integer semantics can otherwise admit `1.0`; this
contract rejects numeric fields written as floats, NaN, Infinity or duplicate keys.
Epoch/consumer allowlists are runtime context, not proof supplied by a payload.
Admission deadlines, clock trust, database uniqueness and transaction boundaries
must still be enforced by the future Pi service.

## Run checks without touching a running service

From this directory:

```sh
python3 -m venv /tmp/smartchair-mqtt-contract-venv
/tmp/smartchair-mqtt-contract-venv/bin/python -m pip install -r requirements.txt
/tmp/smartchair-mqtt-contract-venv/bin/python -m unittest discover -s tests -v
```

The broker test starts its **own** Mosquitto process on an automatically allocated
loopback port, with a temporary config/database and test-only anonymous accounts.
It never connects to the existing broker on 1883/8883 and never starts/stops an
installed service. It tests actual MQTT receipts, both durable input topic families
and a fresh backend process using the same client ID and existing broker session.
It is skipped when the `mosquitto` executable is unavailable. It cleans up only
its own subprocesses. Unit tests also inject receipt-write failures, changed
content under reused identities, lost receipts and native ACK starvation.

For a single payload file, validate publisher-side flags:

```sh
/tmp/smartchair-mqtt-contract-venv/bin/python contract.py \
  --topic smartchair/v1/lab/nodes/chair-01/commands/config \
  --payload /tmp/current-command.json --qos 1
```

Retained state/status producers pass `--retain`. Subscribers validating those
producer flags must use Retain As Published; retained tombstone handling belongs
to the future publisher/maintenance implementation, not JSON-object validation.

## Run the backend example manually

Point this at a **test broker** and a dedicated backend database, never the Pi's
protocol database. The loopback plaintext profile must be explicitly enabled:

```sh
/tmp/smartchair-mqtt-contract-venv/bin/python backend_receipt_example.py \
  --broker 127.0.0.1 --port 18884 --allow-local-plaintext \
  --site lab --consumer backend-01 --bridge pi-01 \
  --db /tmp/smartchair-backend-example.sqlite
```

For a remote authenticated broker, omit plaintext mode, select a trusted CA with
`--ca-file`, and supply `MQTT_USERNAME` / `MQTT_PASSWORD` through a protected
credential environment. Do not put secrets on the command line or in fixtures.
The broker ACL must restrict receipt writes to the designated backend principal;
an allowlisted `bridge_id` in a payload is not authenticated sender identity.

The worker subscribes only to `nodes/+/events` and `nodes/+/commands/+/updates` in
its site. It commits each exact publication, deduplicated semantic fact and receipt
outbox record atomically with WAL + synchronous FULL. Only after that commit may
it native-ACK the received MQTT delivery or publish `MQTT_ACK`. A duplicate queues
the same receipt again, allowing recovery after a lost receipt. Broker PUBACK
retires the receipt queue entry, not the archived fact/publication. Invalid data
is diagnosed durably and receives no application receipt. Existing/unrelated
SQLite databases and changes to an example database's configured identity are
rejected rather than reset.

Paho uses a clean session on the first connection of each fresh process and can
resume its still-live native session on socket reconnect. Outgoing native receipt
ACKs and SUBACKs have 30-second deadlines. Callbacks enqueue bounded work; database
and publication work run between calls to the single-thread MQTT loop. Persistent
receipt rejection fails closed with the pending receipt preserved for inspection.
The example is not a complete production supervisor, business-processing backend
or implementation of the Pi's quarantine/reservation/epoch-maintenance machinery.

## Validation boundary

The fixture suite proves the executable payload contract, and the SQLite tests
exercise the reference backend's commit-before-ACK boundary. The optional broker
test checks the actual Paho/Mosquitto exchange and fresh-process recovery. The
specification's future Pi outbox, command-admission retry implementation, disk
reservation enforcement, full broker-negative-ACK classifier and epoch barrier
still require implementation and fault-injection tests. None of these checks
measures RF reliability, sensor timing or physical power consumption.
