"""Transactional backend ingestion and receipt outbox for an application-owned database."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any

from .contract import IDENTIFIER, NODE, ContractError, Route, known_object, validate
from .models import JsonObject
from .store import MAX_REJECTIONS, compact

_FactCallback = Callable[[sqlite3.Connection, Route, JsonObject], object]


class BackendLedger:
    """Deduplicate chair facts and queue backend receipts in the caller's database.

    The caller owns the connection and MQTT client. For exactly-once *database*
    effects, on_fact must use this same connection and must not commit or perform
    external side effects. Acknowledge the incoming MQTT publication only after
    ingest() returns; acknowledge a receipt publication only after broker PUBACK.
    """

    def __init__(
        self,
        db: sqlite3.Connection,
        *,
        node_id: str,
        bridge_id: str,
        consumer_id: str,
        on_fact: _FactCallback | None = None,
    ) -> None:
        if db.in_transaction:
            raise ValueError("backend database must have no open transaction")
        if not isinstance(node_id, str) or not NODE.fullmatch(node_id):  # pyright: ignore[reportUnnecessaryIsInstance]  # runtime guard for untyped callers
            raise ValueError("invalid node ID")
        if not all(
            isinstance(value, str) and IDENTIFIER.fullmatch(value)  # pyright: ignore[reportUnnecessaryIsInstance]  # runtime guard for untyped callers
            for value in (bridge_id, consumer_id)
        ):
            raise ValueError("invalid backend identity")
        if on_fact is not None and not callable(on_fact):
            raise ValueError("on_fact must be callable")
        database_file = db.execute("PRAGMA database_list").fetchone()[2]
        if not database_file:
            raise ValueError("a durable SQLite database is required")
        if db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
            raise ValueError("backend SQLite WAL is unavailable")
        db.execute("PRAGMA synchronous=FULL")
        self.db, self.node_id, self.bridge_id, self.consumer_id, self.on_fact = (
            db,
            node_id,
            bridge_id,
            consumer_id,
            on_fact,
        )
        with self._transaction():
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS chair_mqtt_backend_config (singleton INTEGER PRIMARY KEY CHECK(singleton=1), node TEXT NOT NULL, bridge TEXT NOT NULL, consumer TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS chair_mqtt_backend_facts (semantic_key TEXT PRIMARY KEY, content_hash TEXT NOT NULL, content BLOB NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS chair_mqtt_backend_deliveries (id TEXT PRIMARY KEY, topic TEXT NOT NULL, payload_hash TEXT NOT NULL, payload BLOB NOT NULL, semantic_key TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS chair_mqtt_backend_receipts (id TEXT PRIMARY KEY, topic TEXT NOT NULL, payload BLOB NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS chair_mqtt_backend_rejections (digest TEXT PRIMARY KEY, topic TEXT NOT NULL, reason TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS chair_mqtt_backend_rejection_counts (kind TEXT PRIMARY KEY, total INTEGER NOT NULL)"
            )
            previous = self.db.execute(
                "SELECT node,bridge,consumer FROM chair_mqtt_backend_config WHERE singleton=1"
            ).fetchone()
            if previous and tuple(previous) != (node_id, bridge_id, consumer_id):
                raise ValueError("backend database has a different chair identity")
            self.db.execute(
                "INSERT OR IGNORE INTO chair_mqtt_backend_config VALUES (1,?,?,?)",
                (node_id, bridge_id, consumer_id),
            )

    @contextmanager
    def _transaction(self) -> Generator[None, None, None]:
        if self.db.in_transaction:
            raise RuntimeError(
                "caller must finish its transaction before MQTT ingestion"
            )
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def ingest(
        self, topic: str, payload: bytes, *, qos: int = 1, retain: bool = False
    ) -> bool:
        """Commit a validated event/report, optional application fact, and receipt."""
        route, value = validate(
            topic,
            payload,
            qos=qos,
            retain=retain,
            expected_node=self.node_id,
            expected_bridge=self.bridge_id,
        )
        if route.kind not in {"event", "report_update"}:
            raise ContractError(
                "backend accepts only reliable events and report updates"
            )
        if route.kind == "event":
            message = known_object(value["message"])
            identity = ["event", route.node, message["boot_id"], message["seq"]]
            content = compact(message)
        else:
            identity = ["report", route.node, value["command_id"], value["report_seq"]]
            content = compact(
                {key: item for key, item in value.items() if key != "publication_id"}
            )
        semantic = compact(identity).decode()
        payload_hash = hashlib.sha256(payload).hexdigest()
        content_hash = hashlib.sha256(content).hexdigest()
        publication_id = value["publication_id"]
        receipt_topic = f"smartchair/v1/bridges/{self.bridge_id}/receipts"
        receipt = compact(
            dict(
                schema_version=1,
                type="MQTT_ACK",
                consumer_id=self.consumer_id,
                publication_id=publication_id,
            )
        )
        with self._transaction():
            prior = self.db.execute(
                "SELECT topic,payload_hash FROM chair_mqtt_backend_deliveries WHERE id=?",
                (publication_id,),
            ).fetchone()
            if prior and tuple(prior) != (topic, payload_hash):
                raise ContractError(
                    "publication ID reused with different topic or bytes"
                )
            fact = self.db.execute(
                "SELECT content_hash FROM chair_mqtt_backend_facts WHERE semantic_key=?",
                (semantic,),
            ).fetchone()
            if fact and fact[0] != content_hash:
                raise ContractError("semantic identity reused with different content")
            if not fact:
                self.db.execute(
                    "INSERT INTO chair_mqtt_backend_facts VALUES (?,?,?)",
                    (semantic, content_hash, content),
                )
                if self.on_fact is not None:
                    self.on_fact(self.db, route, value)
            self.db.execute(
                "INSERT OR IGNORE INTO chair_mqtt_backend_deliveries VALUES (?,?,?,?,?)",
                (publication_id, topic, payload_hash, payload, semantic),
            )
            self.db.execute(
                "INSERT OR IGNORE INTO chair_mqtt_backend_receipts VALUES (?,?,?)",
                (publication_id, receipt_topic, receipt),
            )
        return not bool(fact)

    def pending_receipts(self, limit: int = 16) -> list[Any]:
        """Return up to ``limit`` pending (publication ID, topic, payload) rows."""
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be positive")
        return self.db.execute(
            "SELECT id,topic,payload FROM chair_mqtt_backend_receipts ORDER BY rowid LIMIT ?",
            (limit,),
        ).fetchall()

    def reject(self, topic: str, payload: bytes, reason: str) -> None:
        """Durably diagnose a bad delivery without retaining its full content."""
        if not isinstance(topic, str) or not isinstance(payload, bytes):  # pyright: ignore[reportUnnecessaryIsInstance]  # runtime guard for untyped callers
            raise ValueError("topic and payload have invalid types")
        digest = hashlib.sha256()
        digest.update(topic.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(payload)
        self.reject_digest(topic, digest.hexdigest(), reason)

    def reject_digest(self, topic: str, digest: str, reason: str) -> None:
        """Store a bounded diagnostic after hashing the complete topic and body."""
        if (
            not isinstance(topic, str)  # pyright: ignore[reportUnnecessaryIsInstance]  # runtime guard for untyped callers
            or not isinstance(digest, str)  # pyright: ignore[reportUnnecessaryIsInstance]  # runtime guard for untyped callers
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError("invalid rejection identity")
        if not isinstance(reason, str) or not reason:  # pyright: ignore[reportUnnecessaryIsInstance]  # runtime guard for untyped callers
            raise ValueError("invalid rejection reason")
        prefix = topic.encode("utf-8")[:256].decode("utf-8", errors="ignore")
        with self._transaction():
            self.db.execute(
                "INSERT OR IGNORE INTO chair_mqtt_backend_rejections VALUES (?,?,?)",
                (digest, prefix, reason[:128]),
            )
            # A full journal drops its oldest entry instead of refusing the new
            # one, so a stream of bad messages cannot stop acknowledgements.
            excess = (
                self.db.execute(
                    "SELECT count(*) FROM chair_mqtt_backend_rejections"
                ).fetchone()[0]
                - MAX_REJECTIONS
            )
            if excess > 0:
                self.db.execute(
                    "DELETE FROM chair_mqtt_backend_rejections WHERE rowid IN "
                    "(SELECT rowid FROM chair_mqtt_backend_rejections "
                    "ORDER BY rowid LIMIT ?)",
                    (excess,),
                )
            self.db.execute(
                "INSERT INTO chair_mqtt_backend_rejection_counts VALUES ('invalid',1) "
                "ON CONFLICT(kind) DO UPDATE SET total=total+1"
            )

    def confirmed(self, publication_id: str) -> None:
        """Remove a receipt only after its QoS-1 broker PUBACK."""
        with self._transaction():
            self.db.execute(
                "DELETE FROM chair_mqtt_backend_receipts WHERE id=?", (publication_id,)
            )
