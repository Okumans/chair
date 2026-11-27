# Writing a storage connector

`ChairClient` keeps every durable decision in a storage connector. The package
ships `SQLiteStorage` (`chair_mqtt/async_storage.py`); this guide shows how to
write an equivalent connector for another database. The worked example uses
PostgreSQL through [asyncpg](https://magicstack.github.io/asyncpg/), but the
same rules apply to any transactional store.

Read the [library guide](MQTT_LIBRARY.md) first; this document assumes you know
how `ChairClient`, `on_event` and `set_config` behave from the application side.

## What a connector is responsible for

A connector is any object with the async methods of the `Storage` protocol in
`chair_mqtt.async_storage`. No base class is required. It has two jobs:

1. **Backend ledger.** Deduplicate reliable sensor events and report updates,
   run the application callback for each new fact in the same transaction, and
   keep an outbox of receipts (`MQTT_ACK`) that tell the Pi bridge the backend
   has the fact.
2. **Controller ledger.** Store configuration commands before they are sent,
   hand them back for retries until the sensor admits them or they expire, and
   track the latest command report.

The client calls the connector like this:

| Moment | Calls |
| --- | --- |
| `await chair.start()` (or entering `async with chair`) | `initialize(node_id, bridge_id, consumer_id)` once. |
| Reliable event or report update arrives | `ingest(...)`; for reports, also `get_command` and `observe_report`. |
| A delivery fails validation | `reject(topic, payload, reason)`. |
| Worker tick (after each MQTT event, and at least every 250 ms) | `due_commands(now_ms)`, `command_submitted(...)` after each publish, `pending_receipts()`. |
| Broker PUBACK for a receipt | `confirm_receipt(publication_id)`. |
| `chair.set_config(...)` | `enqueue_command(request, now_ms)`. |
| `chair.get_command(...)` / `command.wait()` | `due_commands(now_ms)` then `get_command(command_id)`. |
| `await chair.stop()` (or leaving `async with chair`) | `close()` once, last. |

Worker calls are sequential, but application tasks can call `set_config` and
`get_command` at the same time, so every method must be safe to run concurrently
with the others.

## Errors decide what happens to the MQTT message

The client acknowledges an incoming publication only after the connector returns.
What you raise matters:

- **`chair_mqtt.contract.ContractError`** means the delivery is permanently
  invalid. The client records it with `reject()` and **acknowledges it**, so the
  broker never redelivers it. Raise it only for bad data, such as a publication
  ID or fact identity reused with different content.
- **Any other exception**, such as a lost database connection, a timeout, or the
  application callback failing, leaves the message **unacknowledged** and sets
  `chair.reconnect_required`. The client then drops the connection itself, at
  most once every 30 seconds, and the broker redelivers the message after the
  automatic reconnect. Never wrap infrastructure errors in `ContractError`;
  that silently drops data.

## Reusable helpers

You do not need to reimplement the wire protocol:

- `chair_mqtt.contract.validate(topic, payload, qos=, retain=, expected_node=,
  expected_bridge=)` checks the topic, QoS, retain flag, JSON schema and IDs.
  It returns `(route, value)`, where `route.kind` is `"event"`,
  `"report_update"`, `"report_status"`, and so on, and `value` is the decoded
  dictionary. It raises `ContractError` for invalid input.
- `chair_mqtt.contract.route(topic)` parses a topic without a payload.
- `chair_mqtt.store.compact(value)` produces the canonical JSON bytes (sorted
  keys, no spaces) that the SQLite connector uses for hashing and stored payloads.

## Data the connector must keep

| Record | Key | Purpose |
| --- | --- | --- |
| Identity | singleton | `node_id`, `bridge_id`, `consumer_id`; refuse to open a database bound to other IDs. |
| Facts | semantic key | Event: `["event", node, boot_id, seq]`. Report: `["report", node, command_id, report_seq]`. Stores a content hash so reuse with different content is detected. |
| Deliveries | `publication_id` | Topic and payload hash; the same publication ID with different bytes is a contract error. |
| Receipts | `publication_id` | Outbox of receipt topic and payload, returned in insertion order until confirmed. |
| Rejections | digest | Bounded diagnosis: SHA-256 of topic, `\0` and payload, a topic prefix of at most 256 bytes, and a reason of at most 128 characters. Keep only the newest entries (SQLite keeps 1024) and a running total. |
| Commands | `command_id` | Exact topic and payload bytes, expiry, next attempt, admitted flag, outcome, latest report sequence and report. |
| Reports | `(command_id, report_seq)` | Content hash for report deduplication. |

The receipt for a fact is published to
`smartchair/v1/bridges/{bridge_id}/receipts` with this payload:

```python
compact({"schema_version": 1, "type": "MQTT_ACK",
         "consumer_id": consumer_id, "publication_id": publication_id})
```

## Method rules

All times are UTC Unix milliseconds as `int`.

- **`initialize`** creates the schema, checks the stored identity, and makes the
  connector ready. Fail if it is called twice.
- **`ingest(topic, payload, *, qos, retain, on_fact)`**: validate with
  `expected_node` and `expected_bridge`, and accept only `event` and
  `report_update`. In **one transaction**, check the delivery and fact
  identities; if the fact is new, insert it and `await on_fact(route, value,
  transaction)`; then record the delivery and insert the receipt. Return `True`
  only for a new fact. A duplicate still gets its receipt (the bridge may have
  missed the first one) but does not run the callback. If the callback raises,
  roll everything back and re-raise.
- **`reject`** stores the bounded diagnosis described above. When the table is
  full, delete its oldest entry; never raise because it is full. Raising leaves
  the delivery unacknowledged, so the broker redelivers it, the client
  reconnects every 30 seconds, and after 16 such messages the broker stops
  delivering anything. Raise only when the write itself fails.
- **`pending_receipts`** returns up to 16 `(publication_id, topic, payload)`
  tuples, oldest first, with `payload` as `bytes`. **`confirm_receipt`** deletes
  one.
- **`enqueue_command(request, now_ms)`** serializes the `ConfigRequest` with
  `compact(request.model_dump(mode="json"))` to
  `smartchair/v1/nodes/{node_id}/commands/config`. It validates the request,
  requires `now_ms < expires_at_ms <= now_ms + 86_400_000`, and returns `False`
  for an identical repeat. It raises `ValueError` if the ID is reused with
  different bytes.
- **`due_commands(now_ms)`** first sets `outcome = "admission_unknown"` on
  unadmitted commands whose expiry has passed, then returns unadmitted,
  unexpired commands with `next_attempt_ms <= now_ms` as `(command_id, topic,
  payload, expires_ms)` tuples.
- **`command_submitted(command_id, now_ms)`** sets the next attempt to
  `now_ms + 15000` for a still-pending command and returns whether a row changed.
- **`observe_report(topic, payload, *, qos, retain)`**: an empty payload on a
  `report_status` topic is a retained tombstone, so return `False`. Otherwise
  validate it, raise `ContractError` for an unknown command or a reused sequence
  with different content, and record the `(command_id, report_seq)` hash. If
  `report_seq` is greater than the stored one, mark the command admitted, store
  `status` as the outcome and the report (without `publication_id`), and return
  `True`.
- **`get_command`** returns `None` or `{"admitted": bool, "outcome": str | None,
  "report_seq": int, "report": dict | None}`.
- **`close`** waits for in-flight work and releases resources.

## Example: PostgreSQL connector

Add the driver to your application with `uv add asyncpg`.

```python
"""PostgreSQL storage connector for chair_mqtt.ChairClient."""

import asyncio
import hashlib
import json

import asyncpg

from chair_mqtt import ConfigRequest
from chair_mqtt.contract import ContractError, route as parse_route, validate
from chair_mqtt.store import compact

SCHEMA = """
CREATE TABLE IF NOT EXISTS chair_identity (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    node TEXT NOT NULL, bridge TEXT NOT NULL, consumer TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chair_facts (
    semantic_key TEXT PRIMARY KEY, content_hash TEXT NOT NULL, content BYTEA NOT NULL);
CREATE TABLE IF NOT EXISTS chair_deliveries (
    id TEXT PRIMARY KEY, topic TEXT NOT NULL, payload_hash TEXT NOT NULL,
    semantic_key TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chair_receipts (
    position BIGSERIAL, id TEXT PRIMARY KEY, topic TEXT NOT NULL, payload BYTEA NOT NULL);
CREATE TABLE IF NOT EXISTS chair_rejections (
    position BIGSERIAL, digest TEXT PRIMARY KEY, topic TEXT NOT NULL,
    reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chair_rejection_counts (
    kind TEXT PRIMARY KEY, total BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS chair_commands (
    id TEXT PRIMARY KEY, topic TEXT NOT NULL, payload BYTEA NOT NULL,
    expires_ms BIGINT NOT NULL, next_attempt_ms BIGINT NOT NULL,
    admitted BOOLEAN NOT NULL DEFAULT FALSE, outcome TEXT,
    latest_seq INTEGER NOT NULL DEFAULT 0, latest_report BYTEA);
CREATE TABLE IF NOT EXISTS chair_reports (
    id TEXT NOT NULL, seq INTEGER NOT NULL, content_hash TEXT NOT NULL,
    PRIMARY KEY (id, seq));
"""

CONTROL = {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE",
           "START", "PREPARE", "ABORT"}


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _check_time(now_ms):
    if type(now_ms) is not int or not 0 <= now_ms <= 9007199254740991:
        raise ValueError("invalid UTC time")
    return now_ms


class PostgresTransaction:
    """Application writes that commit or roll back with the chair fact."""

    def __init__(self, connection):
        self._connection = connection
        self._active = True

    def _check(self, query):
        if not self._active:
            raise RuntimeError("transaction is no longer active")
        if query.lstrip().split(None, 1)[0].upper() in CONTROL:
            raise ValueError("transaction control is managed by the connector")

    async def execute(self, query, *args):
        """Run a statement and return its status string, such as 'INSERT 0 1'."""
        self._check(query)
        return await self._connection.execute(query, *args)

    async def fetch(self, query, *args):
        """Run a query and return its rows."""
        self._check(query)
        return await self._connection.fetch(query, *args)


class PostgresStorage:
    """Durable chair storage in PostgreSQL, created from an asyncpg DSN."""

    def __init__(self, dsn, *, min_size=1, max_size=4):
        self._dsn, self._sizes = dsn, (min_size, max_size)
        self._pool = None
        self._callbacks = set()  # Tasks currently running an on_fact callback.

    def _not_in_callback(self):
        # A call awaited from on_fact would wait on the fact's own locks.
        if asyncio.current_task() in self._callbacks:
            raise RuntimeError(
                "storage cannot be used from inside its own transaction callback")

    async def initialize(self, node_id, bridge_id, consumer_id):
        if self._pool is not None:
            raise RuntimeError("storage already initialized")
        pool = await asyncpg.create_pool(
            self._dsn, min_size=self._sizes[0], max_size=self._sizes[1])
        try:
            async with pool.acquire() as db, db.transaction():
                # Serialize concurrent first-time setup from several processes.
                await db.execute("SELECT pg_advisory_xact_lock(7466726)")
                await db.execute(SCHEMA)
                previous = await db.fetchrow(
                    "SELECT node, bridge, consumer FROM chair_identity")
                if previous and tuple(previous) != (node_id, bridge_id, consumer_id):
                    raise ValueError("database has a different chair identity")
                await db.execute(
                    "INSERT INTO chair_identity (node, bridge, consumer) "
                    "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                    node_id, bridge_id, consumer_id)
        except BaseException:
            await pool.close()
            raise
        self.node_id, self.bridge_id, self.consumer_id = node_id, bridge_id, consumer_id
        self._pool = pool

    # Backend ledger

    async def ingest(self, topic, payload, *, qos, retain, on_fact):
        self._not_in_callback()
        route, value = validate(topic, payload, qos=qos, retain=retain,
                                expected_node=self.node_id,
                                expected_bridge=self.bridge_id)
        if route.kind == "event":
            message = value["message"]
            identity = ["event", route.node, message["boot_id"], message["seq"]]
            content = compact(message)
        elif route.kind == "report_update":
            identity = ["report", route.node, value["command_id"], value["report_seq"]]
            content = compact({k: v for k, v in value.items() if k != "publication_id"})
        else:
            raise ContractError("backend accepts only reliable events and report updates")
        semantic = compact(identity).decode()
        publication_id = value["publication_id"]
        receipt = compact(dict(schema_version=1, type="MQTT_ACK",
                               consumer_id=self.consumer_id,
                               publication_id=publication_id))

        async with self._pool.acquire() as db, db.transaction():
            prior = await db.fetchrow(
                "SELECT topic, payload_hash FROM chair_deliveries WHERE id = $1",
                publication_id)
            if prior and tuple(prior) != (topic, _sha256(payload)):
                raise ContractError("publication ID reused with different topic or bytes")
            # ON CONFLICT makes a concurrent duplicate wait for this transaction.
            fresh = await db.fetchval(
                "INSERT INTO chair_facts VALUES ($1, $2, $3) "
                "ON CONFLICT DO NOTHING RETURNING TRUE",
                semantic, _sha256(content), content)
            if not fresh:
                existing = await db.fetchval(
                    "SELECT content_hash FROM chair_facts WHERE semantic_key = $1",
                    semantic)
                if existing != _sha256(content):
                    raise ContractError("semantic identity reused with different content")
            else:
                transaction = PostgresTransaction(db)
                task = asyncio.current_task()
                self._callbacks.add(task)
                try:
                    await on_fact(route, value, transaction)
                finally:
                    self._callbacks.discard(task)
                    transaction._active = False
            await db.execute(
                "INSERT INTO chair_deliveries VALUES ($1, $2, $3, $4) "
                "ON CONFLICT DO NOTHING",
                publication_id, topic, _sha256(payload), semantic)
            await db.execute(
                "INSERT INTO chair_receipts (id, topic, payload) VALUES ($1, $2, $3) "
                "ON CONFLICT DO NOTHING",
                publication_id, f"smartchair/v1/bridges/{self.bridge_id}/receipts",
                receipt)
        return bool(fresh)

    async def reject(self, topic, payload, reason):
        self._not_in_callback()
        if not isinstance(topic, str) or not isinstance(payload, bytes):
            raise ValueError("topic and payload have invalid types")
        if not isinstance(reason, str) or not reason:
            raise ValueError("invalid rejection reason")
        digest = _sha256(topic.encode() + b"\x00" + payload)
        prefix = topic.encode()[:256].decode(errors="ignore")
        async with self._pool.acquire() as db, db.transaction():
            await db.execute("LOCK TABLE chair_rejections IN EXCLUSIVE MODE")
            await db.execute(
                "INSERT INTO chair_rejections (digest, topic, reason) "
                "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                digest, prefix, reason[:128])
            # Keep the newest 1024: a full journal must not stop acknowledgements.
            await db.execute(
                "DELETE FROM chair_rejections WHERE position IN (SELECT position "
                "FROM chair_rejections ORDER BY position DESC OFFSET 1024)")
            await db.execute(
                "INSERT INTO chair_rejection_counts VALUES ('invalid', 1) "
                "ON CONFLICT (kind) DO UPDATE "
                "SET total = chair_rejection_counts.total + 1")

    async def pending_receipts(self):
        self._not_in_callback()
        rows = await self._pool.fetch(
            "SELECT id, topic, payload FROM chair_receipts ORDER BY position LIMIT 16")
        return [tuple(row) for row in rows]

    async def confirm_receipt(self, publication_id):
        self._not_in_callback()
        await self._pool.execute("DELETE FROM chair_receipts WHERE id = $1",
                                 publication_id)

    # Controller ledger

    async def enqueue_command(self, request, now_ms):
        self._not_in_callback()
        now_ms = _check_time(now_ms)
        if not isinstance(request, ConfigRequest):
            raise TypeError("request must be a ConfigRequest")
        if request.message.node_id != self.node_id:
            raise ValueError("request targets another node")
        topic = f"smartchair/v1/nodes/{self.node_id}/commands/config"
        payload = compact(request.model_dump(mode="json"))
        validate(topic, payload, qos=1, retain=False, expected_node=self.node_id)
        if not now_ms < request.expires_at_ms <= now_ms + 86_400_000:
            raise ValueError("request expiry must be within 24 hours")
        command_id = request.message.command_id
        async with self._pool.acquire() as db, db.transaction():
            inserted = await db.fetchval(
                "INSERT INTO chair_commands (id, topic, payload, expires_ms, "
                "next_attempt_ms) VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT DO NOTHING RETURNING TRUE",
                command_id, topic, payload, request.expires_at_ms, now_ms)
            if inserted:
                return True
            previous = await db.fetchrow(
                "SELECT topic, payload FROM chair_commands WHERE id = $1", command_id)
            if tuple(previous) != (topic, payload):
                raise ValueError("command ID reused with different content")
            return False

    async def due_commands(self, now_ms):
        self._not_in_callback()
        now_ms = _check_time(now_ms)
        async with self._pool.acquire() as db, db.transaction():
            await db.execute(
                "UPDATE chair_commands SET outcome = 'admission_unknown' "
                "WHERE NOT admitted AND outcome IS NULL AND expires_ms <= $1", now_ms)
            rows = await db.fetch(
                "SELECT id, topic, payload, expires_ms FROM chair_commands "
                "WHERE NOT admitted AND expires_ms > $1 AND next_attempt_ms <= $1 "
                "ORDER BY expires_ms, id", now_ms)
        return [tuple(row) for row in rows]

    async def command_submitted(self, command_id, now_ms):
        self._not_in_callback()
        now_ms = _check_time(now_ms)
        status = await self._pool.execute(
            "UPDATE chair_commands SET next_attempt_ms = $1 "
            "WHERE id = $2 AND NOT admitted AND expires_ms > $3",
            now_ms + 15000, command_id, now_ms)
        return status == "UPDATE 1"

    async def observe_report(self, topic, payload, *, qos, retain):
        self._not_in_callback()
        if payload == b"":
            target = parse_route(topic)
            if (target.kind != "report_status" or target.node != self.node_id
                    or qos != 1 or type(retain) is not bool):
                raise ContractError("invalid status tombstone")
            return False
        route, value = validate(topic, payload, qos=qos, retain=retain,
                                expected_node=self.node_id)
        if route.kind not in {"report_update", "report_status"}:
            raise ContractError("not a command report")
        command_id, seq = value["command_id"], value["report_seq"]
        content = compact({k: v for k, v in value.items() if k != "publication_id"})
        async with self._pool.acquire() as db, db.transaction():
            # Row lock orders concurrent reports for the same command.
            latest = await db.fetchval(
                "SELECT latest_seq FROM chair_commands WHERE id = $1 FOR UPDATE",
                command_id)
            if latest is None:
                raise ContractError("report does not match a submitted command")
            known = await db.fetchval(
                "SELECT content_hash FROM chair_reports WHERE id = $1 AND seq = $2",
                command_id, seq)
            if known is not None and known != _sha256(content):
                raise ContractError("report sequence reused with different content")
            if known is None:
                await db.execute("INSERT INTO chair_reports VALUES ($1, $2, $3)",
                                 command_id, seq, _sha256(content))
            if seq <= latest:
                return False
            await db.execute(
                "UPDATE chair_commands SET admitted = TRUE, outcome = $1, "
                "latest_seq = $2, latest_report = $3 WHERE id = $4",
                value["status"], seq, content, command_id)
            return True

    async def get_command(self, command_id):
        self._not_in_callback()
        row = await self._pool.fetchrow(
            "SELECT admitted, outcome, latest_seq, latest_report "
            "FROM chair_commands WHERE id = $1", command_id)
        if row is None:
            return None
        return dict(admitted=row["admitted"], outcome=row["outcome"],
                    report_seq=row["latest_seq"],
                    report=json.loads(row["latest_report"])
                    if row["latest_report"] is not None else None)

    async def close(self):
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
```

Use it exactly like `SQLiteStorage`. Note that the callback's transaction now
uses PostgreSQL placeholders:

```python
chair = ChairClient(ClientConfig(node_id="chair-01", bridge_id="pi-01",
                                 consumer_id="backend-01"),
                    PostgresStorage("postgresql://chair@db.example/chair"),
                    BrokerConfig(host="broker.example", client_id="chair-backend"))

@chair.on_event
async def event_received(event, transaction):
    await transaction.execute(
        "INSERT INTO app_events (boot, seq) VALUES ($1, $2)",
        event.message.boot_id, event.message.seq)
```

### Design notes

- **One connection per transaction.** Each method takes its own pooled
  connection, so concurrent `set_config` calls and the client worker do not
  share transaction state. The SQLite connector gets the same effect by running
  everything on one worker thread.
- **Concurrency comes from the database.** `INSERT ... ON CONFLICT DO NOTHING`
  makes the second of two concurrent duplicate ingests wait for the first to
  commit or roll back. `SELECT ... FOR UPDATE` serializes reports for one command.
  A document store or key-value database needs an equivalent conditional write
  or lock.
- **The callback runs inside the transaction.** Keep it short; it holds a pooled
  connection and row locks until it returns. A connector method awaited from
  inside the callback would wait for those locks or for a free pool connection,
  so detect that case and raise, as `SQLiteStorage` and the example do: record
  `asyncio.current_task()` while awaiting `on_fact` and raise `RuntimeError` in
  your methods when the current task is a recorded one. Tasks started by the
  callback are separate and can run after it returns.
- **Commit before the receipt can leave.** The receipt row is written in the
  same transaction as the fact and the application writes, and the client reads
  it only through `pending_receipts()`. A crash at any point either keeps all
  three or none, and the bridge redelivers if the receipt is missing.
- **Durability is the database's job.** Keep PostgreSQL's default
  `synchronous_commit = on`. For other stores, make sure a returned commit
  survives a crash; the SQLite connector enables WAL with `synchronous=FULL`.

## Testing a connector

`tests/test_mqtt_async_storage.py` shows the checks worth copying: the command
lifecycle (`enqueue_command`, `due_commands`, `command_submitted`, expiry to
`admission_unknown`), a failing `on_fact` leaving no receipt or application rows,
and duplicate ingestion invoking the callback once while keeping one receipt.
`MqttStore` from `chair_mqtt.store` can generate valid bridge publications for
those tests without hardware. Point the tests at a disposable database and run
them with `uv run --locked python -m unittest`.
