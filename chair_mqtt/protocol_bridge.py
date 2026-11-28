"""The gateway protocol engine: durable sensor facts, command lifecycle and radio leases.

The MQTT bridge and the radio-window bench tool build on it; it needs only pyserial.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeGuard, cast

if TYPE_CHECKING:
    from .models import JsonObject
    from .transport import ByteWriter

UINT32_MAX = 0xFFFFFFFF
MAX_BOOT_GENERATION = 0x7FFFFFFFFFFFFFFF
SCHEMA_VERSION = 2
MAX_PENDING_ACKS = 128
RADIO_TIMEOUT_S = 1
PROBE_LEASE_CAP_S = 5
PROBE_RETRY_S = 5
COMMAND_RETRY_S = 0.75
SERIAL_POLL_S = 0.02
SERIAL_WRITE_TIMEOUT_S = 1
RELIABLE = {
    "OCCUPIED",
    "VACATED",
    "HEARTBEAT",
    "STATE_SYNC",
    "BATTERY_LOW",
    "COMMAND_RESULT",
}
STATUSES = {
    "applied",
    "already_applied",
    "revision_conflict",
    "stale_revision",
    "busy",
    "received",
    "command_conflict",
}


def integer(
    value: object, minimum: int = 0, maximum: int = UINT32_MAX
) -> TypeGuard[int]:
    """Check an exact JSON integer, excluding bool, within the inclusive bounds."""
    return type(value) is int and minimum <= value <= maximum


def configuration(config: object) -> bool:
    """Validate every field of a complete configuration without narrowing uint32 limits."""
    if not isinstance(config, dict):
        return False
    config = cast("JsonObject", config)  # Checked as a dict; keys are compared below.
    return (
        set(config)
        == {
            "revision",
            "heartbeat_s",
            "movement_batch_s",
            "movement_enabled",
            "radio_windows_enabled",
            "radio_interval_s",
            "radio_open_s",
            "radio_motion_tail_s",
        }
        and integer(config["revision"])
        and integer(config["heartbeat_s"], 1)
        and integer(config["movement_batch_s"], 1)
        and type(config["movement_enabled"]) is bool
        and type(config["radio_windows_enabled"]) is bool
        and integer(config["radio_interval_s"], 1)
        and integer(config["radio_open_s"], 1, config["radio_interval_s"])
        and integer(config["radio_motion_tail_s"], 1)
    )


def canonical_uuid(text: object) -> str:
    """Return a nonzero canonical UUID string, or raise ValueError."""
    if not isinstance(text, str):
        raise ValueError("invalid command UUID")
    value = uuid.UUID(text)
    if not value.int or str(value) != text:
        raise ValueError("expected nonzero canonical command UUID")
    return text


def mapping_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """Load an explicit node-to-unicast-MAC mapping and reject ambiguous identities."""
    decoded: object = json.loads(Path(path).read_text())
    if not isinstance(decoded, dict) or not decoded:
        raise ValueError("node mapping must be a nonempty node-to-MAC object")
    mapping = cast("JsonObject", decoded)  # A JSON object always has string keys.
    result: dict[str, str] = {}
    for node, mac in mapping.items():
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{1,15}", node)
            or not isinstance(mac, str)
            or not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac)
        ):
            raise ValueError("invalid node mapping")
        if int(mac[:2], 16) & 1 or int(mac.replace(":", ""), 16) == 0:
            raise ValueError("expected unicast station MAC")
        result[node] = mac.lower()
    if len(set(result.values())) != len(result):
        raise ValueError("duplicate station MAC")
    return result


def validate(message: object, mapping: Mapping[str, str]) -> JsonObject:
    """Validate gateway telemetry and its provisioned sender; return the decoded message."""
    if not isinstance(message, dict):
        raise ValueError("expected telemetry")
    # Decoded JSON; every field is checked below.
    envelope = cast("JsonObject", message)
    if envelope.get("kind") != "telemetry":
        raise ValueError("expected telemetry")
    decoded = envelope.get("message")
    if not isinstance(decoded, dict):
        raise ValueError("unknown node")
    m = cast("JsonObject", decoded)  # Decoded JSON; every field is checked below.
    node = m.get("node_id")
    # Mapping keys are strings, so the isinstance test only narrows the type.
    if node not in mapping or not isinstance(node, str):
        raise ValueError("unknown node")
    # A non-string source MAC has always raised AttributeError here; keep that.
    if envelope.get("source_mac", "").lower() != mapping[node]:  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]  # unvalidated JSON
        raise ValueError("sender does not match node")
    if (
        type(m.get("version")) is not int
        or m["version"] != 2
        or not isinstance(boot := m.get("boot_id"), str)
        or not re.fullmatch(r"[0-9a-f]{16}", boot)
        or int(boot, 16) == 0
        or not integer(m.get("seq"), 1)
    ):
        raise ValueError("invalid event identity")
    kind = m.get("type")
    if kind not in RELIABLE | {
        "READY",
        "MOVEMENT_BATCH",
        "MOVEMENT_LOSS",
        "COMMAND_RECEIPT",
        "READY_PROOF",
    }:
        raise ValueError("unknown event type")
    generation = m.get("boot_generation")
    if kind == "STATE_SYNC":
        if (
            not isinstance(generation, str)
            or not re.fullmatch(r"[0-9a-f]{16}", generation)
            or int(generation, 16) == 0
            or int(generation, 16) > MAX_BOOT_GENERATION
        ):
            raise ValueError("invalid boot generation")
    elif generation is not None:
        raise ValueError("invalid boot generation")
    if kind != "COMMAND_RECEIPT":
        if (
            "battery_mv" not in m
            or not integer(m.get("uptime_ms"))
            or type(m.get("occupied")) is not bool
            or not integer(m.get("config_revision"))
            or not integer(m.get("health_flags"), 0, 3)
            or (
                m.get("battery_mv") is not None
                and not integer(m["battery_mv"], 0, 65534)
            )
        ):
            raise ValueError("invalid telemetry")
        if (kind == "OCCUPIED" and not m["occupied"]) or (
            kind == "VACATED" and m["occupied"]
        ):
            raise ValueError("event contradicts occupancy")
    if kind in {"COMMAND_RESULT", "COMMAND_RECEIPT"}:
        canonical_uuid(m.get("command_id"))
        if m.get("status") not in STATUSES or (
            kind == "COMMAND_RESULT" and m["status"] == "received"
        ):
            raise ValueError("invalid command status")
    if kind in {"READY", "READY_PROOF"}:
        if (
            "listen_remaining_ms" not in m
            or (
                kind == "READY"
                and m.get("reason")
                not in {
                    "seat_occupied",
                    "movement_started",
                    "scheduled",
                }
            )
            or (
                m.get("listen_remaining_ms") is not None
                and not integer(m["listen_remaining_ms"], 0, UINT32_MAX - 1)
            )
        ):
            raise ValueError("invalid availability")
        if kind == "READY_PROOF" and (
            not m["occupied"]
            or not isinstance(ack_boot := m.get("ack_boot_id"), str)
            or not re.fullmatch(r"[0-9a-f]{16}", ack_boot)
            or int(ack_boot, 16) == 0
            or not integer(m.get("ack_seq"), 1)
        ):
            raise ValueError("invalid availability proof")
    if kind == "MOVEMENT_BATCH":
        samples = m.get("samples")
        if (
            not integer(m.get("batch_start_ms"))
            or not integer(duration := m.get("duration_ms"))
            or m.get("end_reason")
            not in {"interval", "capacity", "stopped", "vacant", "disabled"}
            or not isinstance(samples, list)
            or len(cast("list[object]", samples)) > 75  # Decoded JSON array.
            or (not samples and m["end_reason"] in {"interval", "capacity"})
        ):
            raise ValueError("invalid movement metadata")
        previous = -1
        for item in cast("list[object]", samples):
            if not isinstance(item, dict):
                raise ValueError("invalid movement sample")
            sample = cast("JsonObject", item)  # Decoded JSON; checked below.
            if (
                set(sample) != {"offset_ms", "ax", "ay", "az", "gx", "gy", "gz"}
                or not integer(sample["offset_ms"], previous + 1, min(65535, duration))
                or any(
                    not integer(sample[key], -32768, 32767)
                    for key in ("ax", "ay", "az", "gx", "gy", "gz")
                )
            ):
                raise ValueError("invalid movement sample")
            previous = sample["offset_ms"]
    if kind == "MOVEMENT_LOSS" and not integer(m.get("dropped_batches"), 1):
        raise ValueError("invalid movement loss count")
    return m


def _identity(message: JsonObject) -> tuple[str, str, int]:
    """Return the node, boot and sequence identity that ``validate`` already checked."""
    node, boot, seq = message["node_id"], message["boot_id"], message["seq"]
    # Unreachable for validated messages; restates validate's identity checks for typing.
    if not isinstance(node, str) or not isinstance(boot, str) or type(seq) is not int:
        raise ValueError("invalid event identity")
    return node, boot, seq


def _listen_remaining_ms(message: JsonObject) -> int | None:
    """Return the announced listen time that ``validate`` already checked."""
    remaining = message["listen_remaining_ms"]
    # Unreachable for validated messages; restates validate's availability check.
    if remaining is not None and type(remaining) is not int:
        raise ValueError("invalid availability")
    return remaining


class Store:
    """SQLite durable facts, command lifecycle and boot-aware current physical state."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        _additional_versions: tuple[int, ...] = (),
    ) -> None:
        """Open the explicit database path and enable WAL with FULL synchronization."""
        if not path or str(path) == ":memory:":
            raise ValueError("a durable SQLite file path is required")

        self.db: sqlite3.Connection = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        existing = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
        ).fetchone()
        if version not in (SCHEMA_VERSION, *_additional_versions) and (
            existing or version
        ):
            self.db.close()
            raise ValueError(
                "unsupported database version; explicitly reset test data or use a new database"
            )
        journal = self.db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if journal != "wal":
            self.db.close()
            raise ValueError("SQLite WAL is unavailable for this database")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                node TEXT, boot TEXT, seq INTEGER, payload TEXT NOT NULL, received_at REAL NOT NULL,
                PRIMARY KEY(node,boot,seq));
            CREATE TABLE IF NOT EXISTS boots (
                node TEXT, boot TEXT, generation INTEGER NOT NULL, PRIMARY KEY(node,boot));
            CREATE TABLE IF NOT EXISTS current_state (
                node TEXT PRIMARY KEY, generation INTEGER, seq INTEGER, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY, node TEXT NOT NULL, config TEXT NOT NULL, expires REAL NOT NULL,
                status TEXT NOT NULL, result TEXT);
        """)
        if not version:
            with self.db:
                self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _identity_payload(message: JsonObject) -> JsonObject:
        payload = {key: value for key, value in message.items() if value is not None}
        if message["type"] == "STATE_SYNC":
            payload.setdefault("boot_generation", "0000000000000000")
        return payload

    def _generation(self, message: JsonObject) -> int | None:
        node, boot = message["node_id"], message["boot_id"]
        known = self.db.execute(
            "SELECT generation FROM boots WHERE node=? AND boot=?", (node, boot)
        ).fetchone()
        if message["type"] != "STATE_SYNC":
            return known[0] if known else None

        encoded = message.get("boot_generation") or "0"
        if not isinstance(encoded, str):
            # int() with a base has always rejected a non-string with TypeError.
            raise TypeError("int() can't convert non-string with explicit base")
        generation = int(encoded, 16)
        if generation == 0:
            return None
        if known:
            if known[0] != generation:
                raise ValueError("boot identity changed its generation")
            return generation

        reused = self.db.execute(
            "SELECT boot FROM boots WHERE node=? AND generation=?", (node, generation)
        ).fetchone()
        if reused:
            raise ValueError("boot generation reused; node reprovisioning is required")
        self.db.execute("INSERT INTO boots VALUES (?,?,?)", (node, boot, generation))
        return generation

    def _after_accept(
        self,
        message: JsonObject,
        *,
        prior: sqlite3.Row | None,
        received_at: float,
        generation: int | None,
        now: float,
    ) -> None:
        """Optional transactional integration hook; any error rolls back sensor acceptance."""

    def _after_expire(self, commands: list[sqlite3.Row], now: float) -> None:
        """Optional transactional integration hook for newly expired commands."""

    def accept(self, message: JsonObject, now: float) -> tuple[bool, float]:
        """Commit before returning (fresh, first_received_at); only then may the caller ACK."""
        node, boot, seq = message["node_id"], message["boot_id"], message["seq"]
        payload = json.dumps(message, sort_keys=True, separators=(",", ":"))
        with self.db:
            prior = self.db.execute(
                "SELECT payload,received_at FROM events WHERE node=? AND boot=? AND seq=?",
                (node, boot, seq),
            ).fetchone()
            if prior and self._identity_payload(
                json.loads(prior[0])
            ) != self._identity_payload(message):
                raise ValueError("event identity reused with different content")
            if message["type"] in RELIABLE:
                self.db.execute(
                    "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?)",
                    (node, boot, seq, payload, now),
                )
            generation = self._generation(message)
            if generation and message["type"] not in {
                "COMMAND_RECEIPT",
                "READY",
                "READY_PROOF",
            }:
                self.db.execute(
                    """INSERT INTO current_state VALUES (?,?,?,?) ON CONFLICT(node) DO UPDATE SET
                    generation=excluded.generation,seq=excluded.seq,payload=excluded.payload
                    WHERE excluded.generation>current_state.generation OR
                    (excluded.generation=current_state.generation AND excluded.seq>current_state.seq)""",
                    (node, generation, seq, payload),
                )
            if message["type"] == "COMMAND_RESULT":
                status = (
                    "applied"
                    if message["status"] in {"applied", "already_applied"}
                    else "rejected"
                )
                self.db.execute(
                    """UPDATE commands SET result=CASE
                    WHEN status IN ('applied','rejected') OR
                    (status='expired' AND result IS NOT NULL) THEN result ELSE ? END,
                    status=CASE WHEN status IN
                    ('expired','applied','rejected') THEN status ELSE ? END WHERE id=? AND node=?""",
                    (payload, status, message["command_id"], node),
                )
            if message["type"] == "COMMAND_RECEIPT" and message["status"] == "received":
                self.db.execute(
                    "UPDATE commands SET status='delivered' WHERE id=? AND node=? AND status='queued'",
                    (message["command_id"], node),
                )
            self._after_accept(
                message,
                prior=prior,
                received_at=prior["received_at"] if prior else now,
                generation=generation,
                now=now,
            )
        return not bool(prior), prior["received_at"] if prior else now

    def queue(
        self,
        node: str,
        command_id: str,
        config: JsonObject,
        expires: float,
        now: float,
    ) -> None:
        """Atomically enqueue a complete, unexpired command; conflicting UUID reuse raises ValueError."""
        canonical_uuid(command_id)
        if not configuration(config) or not math.isfinite(expires) or not expires > now:
            raise ValueError("invalid configuration or expiry")
        encoded = json.dumps(config, sort_keys=True, separators=(",", ":"))
        with self.db:
            prior = self.db.execute(
                "SELECT node,config,expires FROM commands WHERE id=?", (command_id,)
            ).fetchone()
            if prior:
                if tuple(prior) != (node, encoded, expires):
                    raise ValueError("command UUID reused with different content")
                return
            self.db.execute(
                "INSERT INTO commands VALUES (?,?,?,?,?,NULL)",
                (command_id, node, encoded, expires, "queued"),
            )

    def pending(self, now: float, monotonic: float | None = None) -> list[sqlite3.Row]:
        """Commit command expiry and return the oldest live command for each node."""
        with self.db:
            expired = self.db.execute(
                "SELECT * FROM commands WHERE expires<=? AND status IN ('queued','delivered')",
                (now,),
            ).fetchall()
            self.db.execute(
                "UPDATE commands SET status='expired' WHERE expires<=? AND status IN ('queued','delivered')",
                (now,),
            )
            self._after_expire(expired, now)
        return self.db.execute("""SELECT * FROM commands c WHERE status IN ('queued','delivered') AND
            rowid=(SELECT MIN(rowid) FROM commands WHERE node=c.node AND status IN ('queued','delivered'))
            ORDER BY rowid""").fetchall()

    def listing(self, now: float) -> list[JsonObject]:
        """Return structured command rows after persisting any newly expired statuses."""
        self.pending(now)
        return [
            dict(row)
            for row in self.db.execute("SELECT * FROM commands ORDER BY rowid")
        ]


@dataclass(frozen=True)
class TelemetryIdentity:
    boot: str
    seq: int
    generation: int | None


@dataclass(frozen=True)
class Availability:
    expires: float
    identity: TelemetryIdentity


@dataclass(frozen=True)
class ReadyProbe:
    boot: str
    seq: int
    sent_at: float
    sensor_boot: str

    def matches(self, message: JsonObject) -> bool:
        return (
            message["ack_boot_id"] == self.boot
            and message["ack_seq"] == self.seq
            and message["boot_id"] == self.sensor_boot
        )


@dataclass(frozen=True)
class CommandDispatch:
    command_id: str
    expires: float
    lease_expires: float


class Bridge:
    """Serial-independent protocol orchestration; availability and ACK queues stay in RAM."""

    def __init__(
        self,
        store: Store,
        mapping: Mapping[str, str],
        *,
        monotonic_clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] | None = None,
    ) -> None:
        """Bind durable storage to explicit node identities and allocate a bridge boot identity."""
        self.store: Store = store
        self.mapping: Mapping[str, str] = mapping
        self.monotonic_clock: Callable[[], float] = monotonic_clock or time.monotonic
        self.wall_clock: Callable[[], float] = wall_clock or time.time
        self.command_dispatch: CommandDispatch | None = None
        self.boot: str = f"{secrets.randbits(64) or 1:016x}"
        self.seq: int = 0
        self.acks: dict[tuple[str, str, int], JsonObject] = {}
        self.available: dict[str, Availability] = {}
        self.ready_hints: dict[str, TelemetryIdentity] = {}
        # A hint only justifies probing while its READY's announced window may still be open.
        self.hint_expires: dict[str, float] = {}
        self.probes: dict[str, ReadyProbe] = {}
        self.last_command: dict[str, float] = {}
        # Keep per-boot watermarks across serial reconnects within this bridge process.
        self.last_telemetry: dict[tuple[str, str], int] = {}
        self.radio_pending: int | None = None
        self.radio_at: float = 0
        self.request: int = secrets.randbelow(UINT32_MAX)
        self.hello_request: int | None = None

    def reset(self) -> JsonObject:
        """Discard connection-local framing work and availability before serial reconnect."""
        self.acks.clear()
        self.available.clear()
        self.ready_hints.clear()
        self.hint_expires.clear()
        self.probes.clear()
        self.radio_pending = None
        self.last_command.clear()
        self.command_dispatch = None
        self.request = self.request % UINT32_MAX + 1
        self.hello_request = self.request
        return {"kind": "hello", "request_id": self.hello_request}

    def clear_available(self, node: str) -> None:
        """Discard a lease together with the identity of the READY that granted it."""
        self.available.pop(node, None)

    def available_until(self, node: str) -> float:
        """Return a lease deadline from its single atomic identity/deadline record."""
        lease = self.available.get(node)
        return lease.expires if lease else 0

    def precedes(
        self, message: JsonObject, reference: TelemetryIdentity | None
    ) -> bool:
        """Identify events known to precede a READY or proof from the same node."""
        if reference is None:
            return False
        node, boot, seq = _identity(message)
        if boot == reference.boot:
            return seq < reference.seq
        row = self.store.db.execute(
            "SELECT generation FROM boots WHERE node=? AND boot=?",
            (node, boot),
        ).fetchone()
        return bool(
            row and reference.generation is not None and row[0] < reference.generation
        )

    def invalidate_before(self, message: JsonObject) -> None:
        """Invalidate older leases and hints independently when a newer fact arrives."""
        node, _, _ = _identity(message)
        lease = self.available.get(node)
        if not self.precedes(message, lease.identity if lease else None):
            self.clear_available(node)
        if not self.precedes(message, self.ready_hints.get(node)):
            self.ready_hints.pop(node, None)
            self.hint_expires.pop(node, None)
            self.probes.pop(node, None)

    def control(self, node: str, kind: str, **fields: object) -> JsonObject:
        """Allocate a typed downstream control identity without changing its target event identity."""
        if self.seq == UINT32_MAX:
            self.boot = f"{secrets.randbits(64) or 1:016x}"
            self.seq = 0
        self.seq += 1
        return dict(
            version=2,
            type=kind,
            node_id=node,
            boot_id=self.boot,
            seq=self.seq,
            **fields,
        )

    def receive(
        self, envelope: JsonObject, now: float, monotonic: float | None = None
    ) -> JsonObject:
        """Validate and durably accept an envelope before making a reliable ACK eligible to send."""
        lease_now = now if monotonic is None else monotonic
        if envelope.get("kind") != "telemetry":
            if (
                envelope.get("kind") == "hello"
                and envelope.get("request_id") == self.hello_request
                and self.hello_request is not None
            ):
                self.hello_request = None
            if (
                envelope.get("kind") in {"tx_result", "error"}
                and envelope.get("request_id") == self.radio_pending
            ):
                self.radio_pending = None
            return envelope
        m = validate(envelope, self.mapping)
        node, boot, seq = _identity(m)
        if m["type"] in {"READY", "READY_PROOF"} and self.hello_request is not None:
            return dict(kind="ignored", reason="awaiting_fresh_connection", message=m)
        if m["type"] == "READY_PROOF":
            # Never persist a proof this bridge cannot correlate with its own probe.
            probe = self.probes.get(node)
            if not probe or not probe.matches(m):
                return dict(kind="ignored", reason="unmatched_ready_proof", message=m)
        fresh, received_at = self.store.accept(m, now)
        identity = (node, boot)
        newer = seq > self.last_telemetry.get(identity, 0)
        # Best-effort batches and loss reports can be sent ahead of an older queued READY;
        # only state-changing reliable events and READY itself invalidate an older lease.
        if newer and (m["type"] in RELIABLE or m["type"] in {"READY", "READY_PROOF"}):
            self.last_telemetry[identity] = seq
        if m["type"] == "COMMAND_RESULT" and m["status"] in {
            "applied",
            "already_applied",
        }:
            self.invalidate_before(m)
        if m["type"] in RELIABLE:
            key = (node, boot, seq)
            if len(self.acks) < MAX_PENDING_ACKS or key in self.acks:
                self.acks[key] = self.control(
                    node,
                    "EVENT_ACK",
                    ack_boot_id=boot,
                    ack_seq=seq,
                )
        if m["type"] != "COMMAND_RECEIPT":
            if not m["occupied"]:
                self.invalidate_before(m)
            # The sensor drains reliable events before best-effort frames, so a matched proof can
            # trail newer reliable facts. Any fact that ends availability also drops the probe.
            elif (m["type"] == "READY" and newer) or m["type"] == "READY_PROOF":
                current = self.store.db.execute(
                    "SELECT generation FROM current_state WHERE node=?", (node,)
                ).fetchone()
                known = self.store.db.execute(
                    "SELECT generation FROM boots WHERE node=? AND boot=?", identity
                ).fetchone()
                if not current or (known and known[0] == current[0]):
                    if m["type"] == "READY":
                        self.ready_hints[node] = TelemetryIdentity(
                            boot, seq, known[0] if known else None
                        )
                        remaining = _listen_remaining_ms(m)
                        self.hint_expires[node] = (
                            float("inf")
                            if remaining is None
                            else lease_now + remaining / 1000
                        )
                    else:
                        probe = self.probes[node]  # Correlation was validated above.
                        self.probes.pop(node, None)
                        remaining = _listen_remaining_ms(m)
                        # The probe left the Pi before the sensor calculated this duration.
                        # Its complete round trip is therefore a conservative age bound.
                        expires = probe.sent_at + min(
                            PROBE_LEASE_CAP_S,
                            PROBE_LEASE_CAP_S
                            if remaining is None
                            else remaining / 1000,
                        )
                        self.clear_available(node)
                        if expires > lease_now:
                            self.available[node] = Availability(
                                expires,
                                TelemetryIdentity(
                                    boot, seq, known[0] if known else None
                                ),
                            )
        return dict(
            kind="accepted", duplicate=not fresh, received_at=received_at, message=m
        )

    def next(
        self, now: float | None = None, monotonic: float | None = None
    ) -> JsonObject | None:
        """Return one ACK or live command when the radio slot is free; expired commands never dispatch."""
        started = self.monotonic_clock()
        supplied_now = now
        now = self.wall_clock() if now is None else now
        if monotonic is not None:
            clock = monotonic
        elif supplied_now is not None:
            clock = now  # Explicit times also support deterministic simulation callers.
        else:
            clock = started
        # Mark expired commands even while serial/radio is unavailable.
        pending = (
            self.store.pending(now)
            if monotonic is None
            else self.store.pending(now, monotonic=monotonic)
        )
        # SQLite may wait for a writer or disk I/O. Never authorize from the pre-I/O time.
        elapsed = max(0, self.monotonic_clock() - started)
        now = self.wall_clock() if supplied_now is None else now + elapsed
        clock += elapsed
        if self.radio_pending is not None and clock - self.radio_at < RADIO_TIMEOUT_S:
            return None
        message: JsonObject | None = None
        if self.acks:
            _, control = self.acks.popitem()
            message = dict(kind="event_ack", message=control)
        else:
            for command in pending:
                if command["expires"] <= now:
                    continue
                node = command["node"]
                probe = self.probes.get(node)
                if (
                    self.available_until(node) <= clock
                    and node in self.ready_hints
                    and self.hint_expires.get(node, 0) > clock
                    and (not probe or clock - probe.sent_at >= PROBE_RETRY_S)
                ):
                    control = self.control(node, "READY_PROBE")
                    # control() just stamped this probe with the bridge's boot and seq.
                    self.probes[node] = ReadyProbe(
                        self.boot,
                        self.seq,
                        clock,
                        self.ready_hints[node].boot,
                    )
                    message = dict(kind="ready_probe", message=control)
                    break
                if (
                    self.available_until(node) > clock
                    and clock - self.last_command.get(command["id"], 0)
                    >= COMMAND_RETRY_S
                ):
                    control = self.control(
                        node,
                        "CONFIG_SET",
                        command_id=command["id"],
                        **json.loads(command["config"]),
                    )
                    message = dict(kind="command", message=control)
                    self.command_dispatch = CommandDispatch(
                        command["id"],
                        command["expires"],
                        self.available_until(node),
                    )
                    self.last_command[command["id"]] = clock
                    break
        if message:
            self.request = self.request % UINT32_MAX + 1
            message["request_id"] = self.request
            self.radio_pending = self.request
            self.radio_at = clock
        return message

    def send(
        self,
        port: ByteWriter,
        outgoing: JsonObject,
        *,
        before_write: Callable[[], bool] | None = None,
    ) -> bool:
        """Recheck command deadlines after encoding and before every serial write."""
        from .transport import write_message

        if outgoing["kind"] != "command":
            return write_message(port, outgoing)
        dispatch = self.command_dispatch
        message = outgoing["message"]
        if not isinstance(message, dict):
            # Subscripting a non-dict has always raised TypeError here.
            raise TypeError("command message must be an object")
        control = cast("JsonObject", message)  # Built by control().
        if dispatch is None or control["command_id"] != dispatch.command_id:
            return False

        def allowed() -> bool:
            return (
                self.radio_pending == outgoing["request_id"]
                and self.wall_clock() < dispatch.expires
                and self.monotonic_clock()
                < min(
                    dispatch.lease_expires,
                    # Leases are keyed by string node IDs; anything else has none.
                    self.available_until(node)
                    if isinstance(node := control["node_id"], str)
                    else 0,
                )
                and (before_write is None or before_write())
            )

        sent = write_message(port, outgoing, before_write=allowed)
        if not sent:
            self.radio_pending = None
            self.last_command.pop(dispatch.command_id, None)
        self.command_dispatch = None
        return sent
