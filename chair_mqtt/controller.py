"""Durable controller submission and report tracking helpers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from ._narrow import known_int, known_str
from .contract import NODE, ContractError, validate
from .contract import route as parse_route
from .models import ConfigRequest, JsonObject
from .store import compact


class ControllerLedger:
    """Store exact command submissions and order reports from both MQTT topics.

    The application owns the database connection and MQTT client. Submit only
    after status and updates subscriptions have both been granted QoS 1. The
    caller must persist send attempts and use the same bytes and deadline on retry.
    """

    def __init__(self, db: sqlite3.Connection, *, node_id: str) -> None:
        if db.in_transaction:
            raise ValueError("controller database must have no open transaction")
        if not isinstance(node_id, str) or not NODE.fullmatch(node_id):  # pyright: ignore[reportUnnecessaryIsInstance]  # runtime guard for untyped callers
            raise ValueError("invalid node ID")
        if not db.execute("PRAGMA database_list").fetchone()[2]:
            raise ValueError("a durable SQLite database is required")
        if db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
            raise ValueError("controller SQLite WAL is unavailable")
        db.execute("PRAGMA synchronous=FULL")
        self.db: sqlite3.Connection = db
        self.node_id: str = node_id
        with self._transaction():
            db.execute(
                "CREATE TABLE IF NOT EXISTS chair_mqtt_controller_config (singleton INTEGER PRIMARY KEY CHECK(singleton=1), node TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS chair_mqtt_controller_commands (id TEXT PRIMARY KEY, topic TEXT NOT NULL, payload BLOB NOT NULL, expires_ms INTEGER NOT NULL, next_attempt_ms INTEGER NOT NULL, admitted INTEGER NOT NULL DEFAULT 0, outcome TEXT, latest_seq INTEGER NOT NULL DEFAULT 0, latest_report BLOB)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS chair_mqtt_controller_reports (id TEXT NOT NULL, seq INTEGER NOT NULL, content_hash TEXT NOT NULL, PRIMARY KEY(id,seq))"
            )
            previous = db.execute(
                "SELECT node FROM chair_mqtt_controller_config WHERE singleton=1"
            ).fetchone()
            if previous and previous[0] != node_id:
                raise ValueError("controller database belongs to another node")
            db.execute(
                "INSERT OR IGNORE INTO chair_mqtt_controller_config VALUES (1,?)",
                (node_id,),
            )

    @contextmanager
    def _transaction(self) -> Generator[None, None, None]:
        if self.db.in_transaction:
            raise RuntimeError("caller must finish its transaction first")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    @staticmethod
    def _now(now_ms: object) -> int:
        if type(now_ms) is not int or not 0 <= now_ms <= 9007199254740991:
            raise ValueError("invalid UTC time")
        return now_ms

    def enqueue(self, request: ConfigRequest, *, now_ms: int) -> bool:
        """Persist a complete request before any MQTT publication is attempted."""
        now_ms = self._now(now_ms)
        if not isinstance(request, ConfigRequest):  # pyright: ignore[reportUnnecessaryIsInstance]  # runtime guard for untyped callers
            raise TypeError("request must be a ConfigRequest")
        if request.message.node_id != self.node_id:
            raise ValueError("request targets another node")
        topic = f"smartchair/v1/nodes/{self.node_id}/commands/config"
        payload = compact(request.model_dump(mode="json"))
        validate(topic, payload, qos=1, retain=False, expected_node=self.node_id)
        command_id = request.message.command_id
        with self._transaction():
            previous = self.db.execute(
                "SELECT topic,payload FROM chair_mqtt_controller_commands WHERE id=?",
                (command_id,),
            ).fetchone()
            if previous:
                if tuple(previous) != (topic, payload):
                    raise ValueError("command ID reused with different content")
                return False
            if not now_ms < request.expires_at_ms <= now_ms + 86400000:
                raise ValueError("request expiry must be within 24 hours")
            self.db.execute(
                "INSERT INTO chair_mqtt_controller_commands(id,topic,payload,expires_ms,next_attempt_ms) VALUES (?,?,?,?,?)",
                (command_id, topic, payload, request.expires_at_ms, now_ms),
            )
        return True

    def due(self, *, now_ms: int) -> list[Any]:
        """Find submissions due for retry and record unconfirmed expired requests."""
        now_ms = self._now(now_ms)
        with self._transaction():
            self.db.execute(
                "UPDATE chair_mqtt_controller_commands SET outcome='admission_unknown' WHERE admitted=0 AND outcome IS NULL AND expires_ms<=?",
                (now_ms,),
            )
            return self.db.execute(
                "SELECT id,topic,payload,expires_ms FROM chair_mqtt_controller_commands WHERE admitted=0 AND expires_ms>? AND next_attempt_ms<=? ORDER BY rowid",
                (now_ms, now_ms),
            ).fetchall()

    def submitted(self, command_id: str, *, now_ms: int) -> bool:
        """Schedule the next identical application retry 15 seconds later."""
        now_ms = self._now(now_ms)
        with self._transaction():
            result = self.db.execute(
                "UPDATE chair_mqtt_controller_commands SET next_attempt_ms=? WHERE id=? AND admitted=0 AND expires_ms>?",
                (now_ms + 15000, command_id, now_ms),
            )
        return result.rowcount == 1

    def observe(
        self, topic: str, payload: bytes, *, qos: int = 1, retain: bool = False
    ) -> bool:
        """Durably deduplicate status/update reports and advance current state."""
        if payload == b"":
            # A retained status tombstone is not a command report or admission.
            target = parse_route(topic)
            if (
                target.kind != "report_status"
                or target.node != self.node_id
                or type(qos) is not int
                or qos != 1
                or type(retain) is not bool
            ):
                raise ContractError("invalid status tombstone")
            return False
        route, value = validate(
            topic, payload, qos=qos, retain=retain, expected_node=self.node_id
        )
        if route.kind not in {"report_update", "report_status"}:
            raise ContractError("not a command report")
        # validate() guarantees a report's UUID and integer sequence.
        command_id = known_str(value["command_id"])
        seq = known_int(value["report_seq"])
        content = compact(
            {key: item for key, item in value.items() if key != "publication_id"}
        )
        digest = hashlib.sha256(content).hexdigest()
        with self._transaction():
            current = self.db.execute(
                "SELECT latest_seq FROM chair_mqtt_controller_commands WHERE id=?",
                (command_id,),
            ).fetchone()
            if current is None:
                raise ContractError("report does not match a submitted command")
            previous = self.db.execute(
                "SELECT content_hash FROM chair_mqtt_controller_reports WHERE id=? AND seq=?",
                (command_id, seq),
            ).fetchone()
            if previous and previous[0] != digest:
                raise ContractError("report sequence reused with different content")
            if not previous:
                self.db.execute(
                    "INSERT INTO chair_mqtt_controller_reports VALUES (?,?,?)",
                    (command_id, seq, digest),
                )
            if seq > current[0]:
                self.db.execute(
                    "UPDATE chair_mqtt_controller_commands SET admitted=1,outcome=?,latest_seq=?,latest_report=? WHERE id=?",
                    (value["status"], seq, content, command_id),
                )
                return True
        return False

    def get(self, command_id: str) -> JsonObject | None:
        """Return admission, outcome and latest report for an ID, or None if unknown."""
        row = self.db.execute(
            "SELECT admitted,outcome,latest_seq,latest_report FROM chair_mqtt_controller_commands WHERE id=?",
            (command_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(
            admitted=bool(row[0]),
            outcome=row[1],
            report_seq=row[2],
            report=json.loads(row[3]) if row[3] is not None else None,
        )
