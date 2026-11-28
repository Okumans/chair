"""Transactional MQTT state sharing the existing chair protocol database."""

import hashlib
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import time
import uuid
from collections import deque
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from .contract import (
    IDENTIFIER,
    NODE,
    SCHEMA,
    known_int,
    known_object,
    known_str,
    validate,
)
from .models import JsonObject
from .protocol_bridge import RELIABLE, SCHEMA_VERSION, Store, configuration

MQTT_SCHEMA_VERSION = 3
MAX_OUTBOX_ROWS = 10_000
MAX_OUTBOX_BYTES = 64 * 1024 * 1024
MAX_BEST_EFFORT = 128
MAX_COMMANDS = 64
MAX_REPORT_SIZE = 16384
MIN_FREE_DISK = 64 * 1024 * 1024
MAX_REJECTIONS = 1024
LOG = logging.getLogger(__name__)
MAX_PROJECTION_ROWS = 1024
MAX_PROJECTION_BYTES = 32 * 1024 * 1024
TERMINAL = {"applied", "rejected", "expired"}
# Keyed by object: lookups use a decoded JSON value, which need not be a string.
RELIABLE_SCHEMAS: dict[object, str] = {
    "OCCUPIED": "esp_occupied",
    "VACATED": "esp_vacated",
    "HEARTBEAT": "esp_heartbeat",
    "STATE_SYNC": "esp_state_sync",
    "BATTERY_LOW": "esp_battery_low",
    "COMMAND_RESULT": "esp_command_result",
}


class CommandAdmissionError(ValueError):
    """A structurally valid command cannot be admitted."""

    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason, self.retryable = reason, retryable


def compact(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def compact_sensor_event(value: JsonObject) -> JsonObject:
    """Remove unrelated legacy nulls without hiding unknown non-null data."""
    schema_name = RELIABLE_SCHEMAS.get(value.get("type"))
    if schema_name is None:
        raise ValueError("event has an unsupported type")
    result = dict(value)
    allowed: JsonObject = SCHEMA["$defs"][schema_name]["properties"]
    for key in set(result) - set(allowed):
        if result[key] is not None:
            raise ValueError("event contains an unknown field")
        del result[key]
    return result


# Outbox states that still need MQTT work; 'completed' rows are tombstones.
UNFINISHED = "('pending','inflight','waiting','quarantined')"


def _written_by_protocol_bridge(path: str | os.PathLike[str]) -> bool:
    """Whether ``path`` holds facts or commands from the retired protocol-only bridge.

    A schema-2 file without data is only an interrupted new database and is used.
    """
    if not Path(path).exists():
        return False
    with closing(sqlite3.connect(path)) as db:
        if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            return False
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        return any(
            db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            for table in ("events", "boots", "current_state", "commands")
            if table in tables
        )


class MqttStore(Store):
    """Extend sensor acceptance and MQTT outbox insertion in one transaction.

    A new database is created at the MQTT schema. A database holding data from
    the retired protocol-only bridge is refused unchanged, and the bare protocol
    ``Store`` used by the bench tools refuses an MQTT database.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        bridge_id: str,
        consumer_id: str,
        mapping: dict[str, str],
        clock_trusted: Callable[[], bool],
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(bridge_id, str) or not IDENTIFIER.fullmatch(bridge_id):  # pyright: ignore[reportUnnecessaryIsInstance]  # runtime guard for untyped callers
            raise ValueError("invalid bridge ID")
        if not isinstance(consumer_id, str) or not IDENTIFIER.fullmatch(consumer_id):  # pyright: ignore[reportUnnecessaryIsInstance]  # runtime guard for untyped callers
            raise ValueError("invalid consumer ID")
        if (
            not callable(clock_trusted)
            or not callable(monotonic_clock)
            or (wall_clock is not None and not callable(wall_clock))
        ):
            raise ValueError("clock callbacks must be callable")
        if not isinstance(mapping, dict) or len(mapping) != 1:  # pyright: ignore[reportUnnecessaryIsInstance]  # runtime guard for untyped callers
            raise ValueError("exactly one provisioned sensor is required")
        for node, mac in mapping.items():
            if not isinstance(node, str) or not NODE.fullmatch(node):  # pyright: ignore[reportUnnecessaryIsInstance]  # runtime guard for untyped callers
                raise ValueError("invalid node ID")
            if not isinstance(mac, str) or not re.fullmatch(  # pyright: ignore[reportUnnecessaryIsInstance]  # runtime guard for untyped callers
                r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac
            ):
                raise ValueError("invalid sensor MAC")
            try:
                address = bytes.fromhex(mac.replace(":", ""))
            except ValueError as error:
                raise ValueError("invalid sensor MAC") from error
            if len(address) != 6 or not any(address) or address[0] & 1:
                raise ValueError("invalid sensor MAC")
        self.path = Path(path)
        self.bridge_id, self.consumer_id = bridge_id, consumer_id
        self.mapping = {node: mac.lower() for node, mac in mapping.items()}
        self.clock_trusted = clock_trusted
        self.monotonic_clock = monotonic_clock
        self.wall_clock = wall_clock
        self.best_effort: deque[tuple[str, bytes]] = deque()
        self.best_effort_dropped = 0
        self._command_deadlines: dict[str, float] = {}
        self._warned_foreign_receipt = False
        if _written_by_protocol_bridge(path):
            raise ValueError(
                "this database was written by the retired protocol-only bridge, "
                "which the MQTT bridge does not upgrade; use a new database path"
            )
        super().__init__(path, _additional_versions=(MQTT_SCHEMA_VERSION,))
        try:
            self._migrate()
        except Exception:
            self.db.close()
            raise

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        self.db.execute("BEGIN IMMEDIATE")
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS mqtt_config (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                bridge_id TEXT NOT NULL, consumer_id TEXT NOT NULL, node TEXT NOT NULL,
                mac TEXT NOT NULL
            )""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS mqtt_outbox (
                publication_id TEXT PRIMARY KEY, logical_key TEXT UNIQUE NOT NULL,
                topic TEXT NOT NULL, payload BLOB NOT NULL, consumer_id TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                retry_at_ms INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT
            )""")
            # Unfinished work is found by state and due time without scanning the
            # completion tombstones the specification requires us to keep.
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS mqtt_outbox_due ON mqtt_outbox(state,retry_at_ms)"
            )
            self.db.execute("""CREATE TABLE IF NOT EXISTS mqtt_projection (
                topic TEXT PRIMARY KEY, desired BLOB, inflight BLOB, dirty INTEGER NOT NULL
            )""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS mqtt_fact_meta (
                node TEXT NOT NULL, boot TEXT NOT NULL, seq INTEGER NOT NULL,
                received_at_ms INTEGER, PRIMARY KEY(node,boot,seq)
            )""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS mqtt_commands (
                id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, expires_ms INTEGER NOT NULL,
                report_seq INTEGER NOT NULL DEFAULT 0, report BLOB,
                reserved INTEGER NOT NULL, retention_state TEXT NOT NULL DEFAULT 'active',
                terminal_ms INTEGER, terminal_order INTEGER,
                broker_terminal INTEGER NOT NULL DEFAULT 0
            )""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS mqtt_responses (
                command_id TEXT PRIMARY KEY, topic TEXT NOT NULL, payload BLOB NOT NULL
            )""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS mqtt_errors (
                id TEXT PRIMARY KEY, topic TEXT NOT NULL, payload BLOB NOT NULL
            )""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS mqtt_rejections (
                digest TEXT PRIMARY KEY, topic TEXT NOT NULL, reason TEXT NOT NULL
            )""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS mqtt_rejection_counts (
                kind TEXT PRIMARY KEY, total INTEGER NOT NULL
            )""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS mqtt_failures (
                kind TEXT NOT NULL, item_key TEXT NOT NULL, code INTEGER NOT NULL,
                attempts INTEGER NOT NULL, PRIMARY KEY(kind,item_key,code)
            )""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS mqtt_quarantine (
                kind TEXT NOT NULL, item_key TEXT NOT NULL, reason TEXT NOT NULL,
                PRIMARY KEY(kind,item_key)
            )""")
            node, mac = next(iter(self.mapping.items()))
            current = self.db.execute(
                "SELECT bridge_id,consumer_id,node,mac FROM mqtt_config"
            ).fetchone()
            if current and tuple(current) != (
                self.bridge_id,
                self.consumer_id,
                node,
                mac,
            ):
                raise ValueError(
                    "database belongs to a different MQTT identity or sensor"
                )
            self.db.execute(
                "INSERT OR IGNORE INTO mqtt_config VALUES (1,?,?,?,?)",
                (self.bridge_id, self.consumer_id, node, mac),
            )
            if version == SCHEMA_VERSION:
                self.db.execute(f"PRAGMA user_version={MQTT_SCHEMA_VERSION}")

    def _millis(self, seconds: object) -> int | None:
        if not self.clock_trusted():
            return None
        if not isinstance(seconds, (int, float)) or not math.isfinite(seconds):
            raise ValueError("invalid receipt timestamp")
        result = int(seconds * 1000)
        if not 0 <= result <= 9007199254740991:
            raise ValueError("receipt timestamp outside range")
        return result

    def _envelope(
        self, message: JsonObject, received_at: object, publication_id: str | None
    ) -> JsonObject:
        node, boot = message["node_id"], message["boot_id"]
        row = self.db.execute(
            "SELECT generation FROM boots WHERE node=? AND boot=?", (node, boot)
        ).fetchone()
        metadata = self.db.execute(
            "SELECT received_at_ms FROM mqtt_fact_meta WHERE node=? AND boot=? AND seq=?",
            (node, boot, message["seq"]),
        ).fetchone()
        return {
            "schema_version": 1,
            "bridge_id": self.bridge_id,
            "publication_id": publication_id,
            "source_mac": self.mapping[known_str(node)],
            "received_at_ms": metadata[0]
            if metadata is not None
            else self._millis(received_at),
            "boot_generation": f"{row[0]:016x}" if row else None,
            "message": message,
        }

    def _insert_outbox(
        self,
        logical_key: str,
        topic: str,
        value: JsonObject,
        *,
        reserved_command: object = None,
    ) -> None:
        if self.db.execute(
            "SELECT 1 FROM mqtt_outbox WHERE logical_key=?", (logical_key,)
        ).fetchone():
            return
        if shutil.disk_usage(self.path.resolve().parent).free < MIN_FREE_DISK:
            raise sqlite3.OperationalError(
                "MQTT storage is below its free-disk safety threshold"
            )
        publication_id = str(uuid.uuid4())
        value["publication_id"] = publication_id
        payload = compact(value)
        validate(
            topic, payload, qos=1, retain=False, expected_node=next(iter(self.mapping))
        )
        if reserved_command is not None:
            self.db.execute(
                "UPDATE mqtt_commands SET reserved=reserved-1 WHERE id=? AND reserved>0",
                (reserved_command,),
            )
        rows, size = self.db.execute(
            f"SELECT count(*),coalesce(sum(length(payload)),0) FROM mqtt_outbox WHERE state IN {UNFINISHED}"
        ).fetchone()
        reserved = self.db.execute(
            "SELECT coalesce(sum(reserved),0) FROM mqtt_commands"
        ).fetchone()[0]
        if (
            rows + reserved + 1 > MAX_OUTBOX_ROWS
            or size + reserved * MAX_REPORT_SIZE + len(payload) > MAX_OUTBOX_BYTES
        ):
            raise sqlite3.OperationalError("MQTT durable outbox is full")
        self.db.execute(
            "INSERT INTO mqtt_outbox(publication_id,logical_key,topic,payload,consumer_id) VALUES (?,?,?,?,?)",
            (publication_id, logical_key, topic, payload, self.consumer_id),
        )

    def _queue_event(self, message: JsonObject, received_at: object) -> None:
        message = compact_sensor_event(message)
        node, boot, seq = message["node_id"], message["boot_id"], message["seq"]
        topic = f"smartchair/v1/nodes/{node}/events"
        self._insert_outbox(
            f"event:{node}:{boot}:{seq}",
            topic,
            self._envelope(message, received_at, None),
            reserved_command=message.get("command_id")
            if message["type"] == "COMMAND_RESULT"
            else None,
        )

    def _refresh_state(self, node: str) -> None:
        row = self.db.execute(
            """SELECT e.payload,e.received_at FROM events e
            JOIN boots b ON b.node=e.node AND b.boot=e.boot WHERE e.node=?
            ORDER BY b.generation DESC,e.seq DESC LIMIT 1""",
            (node,),
        ).fetchone()
        if row is None:
            return
        message = compact_sensor_event(json.loads(row["payload"]))
        topic = f"smartchair/v1/nodes/{node}/state"
        payload = compact(self._envelope(message, row["received_at"], None))
        validate(topic, payload, qos=1, retain=True, expected_node=node)
        self._queue_projection(topic, payload)

    def _queue_projection(
        self, topic: str, payload: bytes, *, force: bool = False
    ) -> None:
        """Queue or coalesce a projection without exceeding its separate pool."""
        existing = self.db.execute(
            "SELECT desired,inflight,dirty FROM mqtt_projection WHERE topic=?", (topic,)
        ).fetchone()
        if existing and existing["desired"] == payload and not force:
            return
        # Reserve a projection slot and enough bytes for both desired and
        # in-flight status for each accepted command until its terminal report
        # is recorded. A clean retained status still needs room for its next
        # transition even if unrelated topics fill the pending projection pool.
        reserved_topic = self.db.execute(
            """SELECT EXISTS(SELECT 1 FROM commands c
                 JOIN mqtt_commands m ON m.id=c.id
                 WHERE m.terminal_order IS NULL AND (m.reserved>0 OR m.report IS NOT NULL) AND
                 ?='smartchair/v1/nodes/'||c.node||'/commands/'||c.id||'/status')""",
            (topic,),
        ).fetchone()[0]
        count, size = self.db.execute(
            """SELECT count(*),coalesce(sum(CASE WHEN m.terminal_order IS NULL AND
                      (m.reserved>0 OR m.report IS NOT NULL)
                      THEN max(32768,coalesce(length(p.desired),0)+coalesce(length(p.inflight),0))
                      ELSE coalesce(length(p.desired),0)+coalesce(length(p.inflight),0) END),0)
               FROM mqtt_projection p
               LEFT JOIN commands c ON p.topic='smartchair/v1/nodes/'||c.node||'/commands/'||c.id||'/status'
               LEFT JOIN mqtt_commands m ON m.id=c.id
               WHERE p.dirty=1 OR p.inflight IS NOT NULL OR
                 (m.terminal_order IS NULL AND (m.reserved>0 OR m.report IS NOT NULL))"""
        ).fetchone()
        if existing and (
            existing["dirty"] or existing["inflight"] is not None or reserved_topic
        ):
            count -= 1
            previous_size = len(existing["desired"] or b"") + len(
                existing["inflight"] or b""
            )
            size -= max(32768, previous_size) if reserved_topic else previous_size
        count += 1
        next_size = len(payload or b"") + (
            len(existing["inflight"] or b"") if existing else 0
        )
        size += max(32768, next_size) if reserved_topic else next_size
        if count > MAX_PROJECTION_ROWS or size > MAX_PROJECTION_BYTES:
            raise sqlite3.OperationalError("MQTT retained projection pool is full")
        self.db.execute(
            """INSERT INTO mqtt_projection(topic,desired,inflight,dirty) VALUES (?,?,NULL,1)
            ON CONFLICT(topic) DO UPDATE SET desired=excluded.desired,dirty=1""",
            (topic, payload),
        )

    def _report(
        self,
        command_id: str,
        status: str,
        now: float,
        *,
        message: JsonObject | None = None,
        late_result: bool = False,
    ) -> None:
        if message is not None:
            message = compact_sensor_event(message)
        row = self.db.execute(
            """SELECT c.node,c.config,m.expires_ms,m.report_seq,m.retention_state,m.terminal_order
               FROM commands c JOIN mqtt_commands m ON c.id=m.id WHERE c.id=?""",
            (command_id,),
        ).fetchone()
        if row is None:
            return
        if row["report_seq"] >= 9007199254740991:
            raise sqlite3.OperationalError("command report sequence exhausted")
        seq = row["report_seq"] + 1
        report = {
            "schema_version": 1,
            "type": "COMMAND_STATUS",
            "bridge_id": self.bridge_id,
            "publication_id": None,
            "command_id": command_id,
            "report_seq": seq,
            "status": status,
            "requested_revision": json.loads(row["config"])["revision"],
            "expires_at_ms": row["expires_ms"],
            "updated_at_ms": self._millis(now),
            "late_result": late_result,
            "reason": None,
            "message": message,
        }
        prefix = f"smartchair/v1/nodes/{row['node']}/commands/{command_id}"
        self._insert_outbox(
            f"report:{row['node']}:{command_id}:{seq}",
            prefix + "/updates",
            dict(report),
            reserved_command=command_id if row["report_seq"] else None,
        )
        projection = compact(report)
        validate(
            prefix + "/status",
            projection,
            qos=1,
            retain=True,
            expected_node=row["node"],
        )
        self.db.execute(
            "UPDATE mqtt_commands SET report_seq=?,report=? WHERE id=?",
            (seq, projection, command_id),
        )
        if row["retention_state"] == "active":
            self._queue_projection(prefix + "/status", projection)
        if status in TERMINAL:
            if row["terminal_order"] is None:
                order = self.db.execute(
                    "SELECT coalesce(max(terminal_order),0)+1 FROM mqtt_commands"
                ).fetchone()[0]
                self.db.execute(
                    "UPDATE mqtt_commands SET terminal_ms=?,terminal_order=? WHERE id=?",
                    (self._millis(now), order, command_id),
                )
            self.db.execute(
                "UPDATE mqtt_commands SET reserved=0 WHERE id=?", (command_id,)
            )

    def admit_command(
        self,
        topic: str,
        payload: bytes,
        *,
        now: float,
        qos: int = 1,
        retain: bool = False,
    ) -> str:
        try:
            return self._admit_command(topic, payload, now=now, qos=qos, retain=retain)
        except sqlite3.OperationalError as error:
            if str(error) in {
                "MQTT durable outbox is full",
                "MQTT retained projection pool is full",
            }:
                raise CommandAdmissionError("queue_full", retryable=True) from error
            raise

    def _admit_command(
        self,
        topic: str,
        payload: bytes,
        *,
        now: float,
        qos: int = 1,
        retain: bool = False,
    ) -> str:
        """Durably admit one MQTT configuration command or an identical retry."""
        node = next(iter(self.mapping))
        target, value = validate(
            topic, payload, qos=qos, retain=retain, expected_node=node
        )
        if target.kind != "command":
            raise CommandAdmissionError("invalid_schema")
        # The command schema requires these shapes; validate() has checked them.
        command = known_object(value["message"])
        command_id, expires = (
            known_str(command["command_id"]),
            known_int(value["expires_at_ms"]),
        )
        fingerprint = hashlib.sha256(compact(value)).hexdigest()
        config: JsonObject = {
            key: command[key]
            for key in (
                "revision",
                "heartbeat_s",
                "movement_batch_s",
                "movement_enabled",
                "radio_windows_enabled",
                "radio_interval_s",
                "radio_open_s",
                "radio_motion_tail_s",
            )
        }
        if not configuration(config):
            raise CommandAdmissionError("invalid_config")
        self.db.execute("BEGIN IMMEDIATE")
        with self.db:
            existing = self.db.execute(
                "SELECT request_hash,retention_state,report FROM mqtt_commands WHERE id=?",
                (command_id,),
            ).fetchone()
            if existing:
                if existing["request_hash"] != fingerprint:
                    raise CommandAdmissionError("command_id_conflict")
                status_topic = (
                    f"smartchair/v1/nodes/{node}/commands/{command_id}/status"
                )
                if existing["retention_state"] in {"pending", "deleted"}:
                    previous = self.db.execute(
                        "SELECT length(payload) FROM mqtt_responses WHERE command_id=?",
                        (command_id,),
                    ).fetchone()
                    count, size = self.db.execute(
                        "SELECT count(*),coalesce(sum(length(payload)),0) FROM mqtt_responses"
                    ).fetchone()
                    if (
                        count + (0 if previous else 1) > 1024
                        or size
                        - (previous[0] if previous else 0)
                        + len(existing["report"])
                        > 2 * 1024 * 1024
                    ):
                        raise sqlite3.OperationalError("MQTT response journal is full")
                    self.db.execute(
                        """INSERT INTO mqtt_responses(command_id,topic,payload) VALUES (?,?,?)
                           ON CONFLICT(command_id) DO UPDATE SET payload=excluded.payload""",
                        (command_id, status_topic, existing["report"]),
                    )
                else:
                    self._queue_projection(status_topic, existing["report"], force=True)
                return "duplicate"
            if self.db.execute(
                "SELECT 1 FROM commands WHERE id=?", (command_id,)
            ).fetchone():
                raise CommandAdmissionError("command_id_conflict")
            if self.wall_clock is not None:
                now = max(now, self.wall_clock())
            now_ms = self._millis(now)
            if now_ms is None:
                raise CommandAdmissionError("clock_untrusted", retryable=True)
            if expires > now_ms + 86400000:
                raise CommandAdmissionError("deadline_too_far")
            status = "expired" if expires <= now_ms else "queued"
            if status == "queued":
                count = self.db.execute(
                    "SELECT count(*) FROM commands WHERE status IN ('queued','delivered')"
                ).fetchone()[0]
                if count >= MAX_COMMANDS:
                    raise CommandAdmissionError("queue_full", retryable=True)
            self.db.execute(
                "INSERT INTO commands VALUES (?,?,?,?,?,NULL)",
                (command_id, node, compact(config).decode(), expires / 1000, status),
            )
            self.db.execute(
                "INSERT INTO mqtt_commands(id,request_hash,expires_ms,reserved) VALUES (?,?,?,?)",
                (command_id, fingerprint, expires, 3 if status == "queued" else 0),
            )
            self._report(command_id, status, now)
        if status == "queued":
            self._command_deadlines[command_id] = (
                self.monotonic_clock() + (expires - now_ms) / 1000
            )
        return status

    def reject_command(
        self,
        reason: str,
        *,
        command_id: str | None = None,
        now: float,
        retryable: bool = False,
    ) -> None:
        """Commit a bounded error response before acknowledging bad command input."""
        node = next(iter(self.mapping))
        topic = f"smartchair/v1/nodes/{node}/command_errors"
        error_id = str(uuid.uuid4())
        value = {
            "schema_version": 1,
            "bridge_id": self.bridge_id,
            "error_id": error_id,
            "command_id": command_id,
            "reason": reason,
            "retryable": retryable,
            "received_at_ms": self._millis(now),
        }
        payload = compact(value)
        validate(topic, payload, qos=1, retain=False, expected_node=node)
        with self.db:
            count, size = self.db.execute(
                "SELECT count(*),coalesce(sum(length(payload)),0) FROM mqtt_errors"
            ).fetchone()
            if count >= 1024 or size + len(payload) > 2 * 1024 * 1024:
                raise sqlite3.OperationalError("MQTT error journal is full")
            self.db.execute(
                "INSERT INTO mqtt_errors VALUES (?,?,?)", (error_id, topic, payload)
            )

    def reject_receipt(self, topic: str, payload: bytes, reason: str) -> None:
        """Commit a bounded diagnostic with a digest over the complete bad input."""
        topic_bytes = topic.encode("utf-8")
        hasher = hashlib.sha256()
        hasher.update(topic_bytes)
        hasher.update(b"\x00")
        hasher.update(payload)
        self.reject_receipt_digest(topic, hasher.hexdigest(), reason)

    def reject_receipt_digest(self, topic: str, digest: str, reason: str) -> None:
        """Record a diagnostic without retaining an oversized payload."""
        topic_bytes = topic.encode("utf-8")
        prefix = topic_bytes[:256].decode("utf-8", errors="ignore")
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO mqtt_rejections VALUES (?,?,?)",
                (digest, prefix, reason[:128]),
            )
            # A full journal drops its oldest entry instead of refusing the new
            # one, so a stream of bad messages cannot stop acknowledgements.
            excess = (
                self.db.execute("SELECT count(*) FROM mqtt_rejections").fetchone()[0]
                - MAX_REJECTIONS
            )
            if excess > 0:
                self.db.execute(
                    "DELETE FROM mqtt_rejections WHERE rowid IN "
                    "(SELECT rowid FROM mqtt_rejections ORDER BY rowid LIMIT ?)",
                    (excess,),
                )
            self._count_rejection("invalid")

    def _count_rejection(self, kind: str) -> None:
        self.db.execute(
            "INSERT INTO mqtt_rejection_counts VALUES (?,1) "
            "ON CONFLICT(kind) DO UPDATE SET total=total+1",
            (kind,),
        )

    def rejection_count(self, kind: str) -> int:
        """Return how many deliveries of ``kind`` were rejected over the database's life."""
        row = self.db.execute(
            "SELECT total FROM mqtt_rejection_counts WHERE kind=?", (kind,)
        ).fetchone()
        return row[0] if row else 0

    def accept_receipt(
        self, topic: str, payload: bytes, *, qos: int = 1, retain: bool = False
    ) -> bool:
        """Commit a matching backend receipt; duplicates and unknown IDs are harmless.

        A well-formed receipt from another consumer, such as a second backend
        or a dashboard that should not send receipts, is only counted. Logging
        each one would flush the rejection journal with a routine
        misconfiguration.
        """
        target, value = validate(
            topic, payload, qos=qos, retain=retain, expected_bridge=self.bridge_id
        )
        if target.kind != "receipt":
            raise ValueError("expected backend receipt")
        if value["consumer_id"] != self.consumer_id:
            with self.db:
                self._count_rejection("foreign_consumer")
            if not self._warned_foreign_receipt:
                self._warned_foreign_receipt = True
                LOG.warning(
                    "Ignoring receipts from consumer %r; only %r completes "
                    "publications. Stop the other client sending receipts.",
                    value["consumer_id"],
                    self.consumer_id,
                )
            return False
        with self.db:
            result = self.db.execute(
                "UPDATE mqtt_outbox SET state='completed' WHERE publication_id=? AND consumer_id=? AND state!='completed'",
                (value["publication_id"], self.consumer_id),
            )
        return result.rowcount == 1

    def schedule_retention(self, now: float) -> int:
        """Persist expiry and capacity cleanup without removing broker state early."""
        now_ms = self._millis(now)
        with self.db:
            if now_ms is not None:
                self.db.execute(
                    "UPDATE mqtt_commands SET terminal_ms=? WHERE terminal_order IS NOT NULL AND terminal_ms IS NULL",
                    (now_ms,),
                )
            rows = self.db.execute(
                """SELECT id,terminal_ms FROM mqtt_commands
                   WHERE terminal_order IS NOT NULL AND retention_state='active'
                   ORDER BY terminal_order"""
            ).fetchall()
            due = {
                row["id"]
                for row in rows
                if now_ms is not None
                and row["terminal_ms"] is not None
                and now_ms - row["terminal_ms"] >= 7 * 86400000
            }
            remaining = [row["id"] for row in rows if row["id"] not in due]
            due.update(remaining[: max(0, len(remaining) - 256)])
            node = next(iter(self.mapping))
            for command_id in due:
                topic = f"smartchair/v1/nodes/{node}/commands/{command_id}/status"
                self.db.execute(
                    "UPDATE mqtt_commands SET retention_state='pending' WHERE id=? AND retention_state='active'",
                    (command_id,),
                )
                self._queue_projection(topic, b"")
        return len(due)

    def _after_expire(self, commands: list[sqlite3.Row], now: float) -> None:
        for command in commands:
            self._report(command["id"], "expired", now)

    def pending(self, now: float, monotonic: float | None = None) -> list[sqlite3.Row]:
        """Fence dispatch without trusted UTC and apply exact/monotonic deadlines."""
        now_ms = self._millis(now)
        if now_ms is None:
            return []
        monotonic = self.monotonic_clock() if monotonic is None else monotonic
        if not isinstance(monotonic, (int, float)) or not math.isfinite(monotonic):  # pyright: ignore[reportUnnecessaryIsInstance]  # runtime guard for untyped callers
            raise ValueError("invalid monotonic time")
        with self.db:
            rows = self.db.execute(
                """SELECT c.*,m.expires_ms FROM commands c
                   LEFT JOIN mqtt_commands m ON m.id=c.id
                   WHERE c.status IN ('queued','delivered') ORDER BY c.rowid"""
            ).fetchall()
            expired: list[sqlite3.Row] = []
            for row in rows:
                expiry = row["expires_ms"]
                if expiry is None:
                    expiry = int(row["expires"] * 1000)
                deadline = self._command_deadlines.setdefault(
                    row["id"], monotonic + max(0, expiry - now_ms) / 1000
                )
                if now_ms >= expiry or monotonic >= deadline:
                    expired.append(row)
                    self.db.execute(
                        "UPDATE commands SET status='expired' WHERE id=? AND status IN ('queued','delivered')",
                        (row["id"],),
                    )
            self._after_expire(expired, now)
            for row in expired:
                self._command_deadlines.pop(row["id"], None)
        return self.db.execute(
            """SELECT * FROM commands c WHERE status IN ('queued','delivered') AND
               rowid=(SELECT MIN(rowid) FROM commands WHERE node=c.node
                      AND status IN ('queued','delivered')) ORDER BY rowid"""
        ).fetchall()

    def _after_accept(
        self,
        message: JsonObject,
        *,
        prior: sqlite3.Row | None,
        received_at: float,
        generation: int | None,
        now: float,
    ) -> None:
        if message["type"] in RELIABLE:
            if prior is None:
                self.db.execute(
                    "INSERT INTO mqtt_fact_meta VALUES (?,?,?,?)",
                    (
                        message["node_id"],
                        message["boot_id"],
                        message["seq"],
                        self._millis(received_at),
                    ),
                )
                self._queue_event(message, received_at)
            if generation is not None:
                # Accepted messages passed protocol validation: node_id is a mapped string.
                self._refresh_state(known_str(message["node_id"]))
        if message["type"] in {"COMMAND_RECEIPT", "COMMAND_RESULT"}:
            row = self.db.execute(
                "SELECT report FROM mqtt_commands WHERE id=?", (message["command_id"],)
            ).fetchone()
            if row is None or row["report"] is None:
                return
            report = json.loads(row["report"])
            status = report["status"]
            if message["type"] == "COMMAND_RECEIPT":
                if message["status"] == "received" and status == "queued":
                    self._report(known_str(message["command_id"]), "delivered", now)
            elif prior is None:
                if status in {"queued", "delivered"}:
                    final = (
                        "applied"
                        if message["status"] in {"applied", "already_applied"}
                        else "rejected"
                    )
                    self._report(
                        known_str(message["command_id"]), final, now, message=message
                    )
                elif status == "expired" and not report["late_result"]:
                    self._report(
                        known_str(message["command_id"]),
                        "expired",
                        now,
                        message=message,
                        late_result=True,
                    )

    def accept(self, message: JsonObject, now: float) -> tuple[bool, float]:
        # Advance deadlines before interpreting a newly arrived command result.
        # Otherwise a result received after expiry can incorrectly become an
        # on-time applied/rejected status between two periodic dispatch checks.
        if (
            message["type"] in {"COMMAND_RESULT", "COMMAND_RECEIPT"}
            and self.clock_trusted()
        ):
            self.pending(now)
        result = super().accept(message, now)
        if message["type"] in {"MOVEMENT_BATCH", "MOVEMENT_LOSS"}:
            topic = f"smartchair/v1/nodes/{message['node_id']}/movement"
            if message["type"] == "MOVEMENT_LOSS":
                topic += "/loss"
            payload = compact(self._envelope(message, now, None))
            validate(
                topic,
                payload,
                qos=0,
                retain=False,
                expected_node=known_str(message["node_id"]),
            )
            if len(self.best_effort) >= MAX_BEST_EFFORT:
                self.best_effort.popleft()
                self.best_effort_dropped += 1
            self.best_effort.append((topic, payload))
        return result
