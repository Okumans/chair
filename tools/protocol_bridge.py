"""Durable protocol bridge. Sessions and MQTT belong to the consuming application."""

import argparse
import json
import math
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path

import serial

UINT32_MAX = 0xFFFFFFFF
MAX_BOOT_GENERATION = 0x7FFFFFFFFFFFFFFF
SCHEMA_VERSION = 2
MAX_PENDING_ACKS = 128
RADIO_TIMEOUT_S = 1
COMMAND_RETRY_S = 0.75
RETRY_DELAY_S = 1
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


def integer(value, minimum=0, maximum=UINT32_MAX):
    """Check an exact JSON integer, excluding bool, within the inclusive bounds."""
    return type(value) is int and minimum <= value <= maximum


def configuration(config):
    """Validate every field of a complete configuration without narrowing uint32 limits."""
    return (
        isinstance(config, dict)
        and set(config)
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


def canonical_uuid(text):
    """Return a nonzero canonical UUID string, or raise ValueError."""
    if not isinstance(text, str):
        raise ValueError("invalid command UUID")
    value = uuid.UUID(text)
    if not value.int or str(value) != text:
        raise ValueError("expected nonzero canonical command UUID")
    return text


def mapping_file(path):
    """Load an explicit node-to-unicast-MAC mapping and reject ambiguous identities."""
    mapping = json.loads(Path(path).read_text())
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("node mapping must be a nonempty node-to-MAC object")
    result = {}
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


def validate(message, mapping):
    """Validate gateway telemetry and its provisioned sender; return the decoded message."""
    if not isinstance(message, dict) or message.get("kind") != "telemetry":
        raise ValueError("expected telemetry")
    m = message.get("message")
    if not isinstance(m, dict) or m.get("node_id") not in mapping:
        raise ValueError("unknown node")
    if message.get("source_mac", "").lower() != mapping[m["node_id"]]:
        raise ValueError("sender does not match node")
    if (
        type(m.get("version")) is not int
        or m["version"] != 2
        or not isinstance(m.get("boot_id"), str)
        or not re.fullmatch(r"[0-9a-f]{16}", m["boot_id"])
        or int(m["boot_id"], 16) == 0
        or not integer(m.get("seq"), 1)
    ):
        raise ValueError("invalid event identity")
    kind = m.get("type")
    if kind not in RELIABLE | {
        "READY",
        "MOVEMENT_BATCH",
        "MOVEMENT_LOSS",
        "COMMAND_RECEIPT",
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
    if kind == "READY":
        if (
            "listen_remaining_ms" not in m
            or m.get("reason")
            not in {
                "seat_occupied",
                "movement_started",
                "scheduled",
            }
            or (
                m.get("listen_remaining_ms") is not None
                and not integer(m["listen_remaining_ms"], 0, UINT32_MAX - 1)
            )
        ):
            raise ValueError("invalid availability")
    if kind == "MOVEMENT_BATCH":
        samples = m.get("samples")
        if (
            not integer(m.get("batch_start_ms"))
            or not integer(m.get("duration_ms"))
            or m.get("end_reason")
            not in {"interval", "capacity", "stopped", "vacant", "disabled"}
            or not isinstance(samples, list)
            or len(samples) > 75
            or (not samples and m["end_reason"] in {"interval", "capacity"})
        ):
            raise ValueError("invalid movement metadata")
        previous = -1
        for sample in samples:
            if (
                not isinstance(sample, dict)
                or set(sample) != {"offset_ms", "ax", "ay", "az", "gx", "gy", "gz"}
                or not integer(
                    sample["offset_ms"], previous + 1, min(65535, m["duration_ms"])
                )
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


class Store:
    """SQLite durable facts, command lifecycle and boot-aware current physical state."""

    def __init__(self, path):
        """Open the explicit database path and enable WAL with FULL synchronization."""
        if not path or str(path) == ":memory:":
            raise ValueError("a durable SQLite file path is required")

        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        existing = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
        ).fetchone()
        if version != SCHEMA_VERSION and (existing or version):
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
    def _identity_payload(message):
        payload = {key: value for key, value in message.items() if value is not None}
        if message["type"] == "STATE_SYNC":
            payload.setdefault("boot_generation", "0000000000000000")
        return payload

    def _generation(self, message):
        node, boot = message["node_id"], message["boot_id"]
        known = self.db.execute(
            "SELECT generation FROM boots WHERE node=? AND boot=?", (node, boot)
        ).fetchone()
        if message["type"] != "STATE_SYNC":
            return known[0] if known else None

        generation = int(message.get("boot_generation") or "0", 16)
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

    def accept(self, m, now):
        """Commit before returning (fresh, first_received_at); only then may the caller ACK."""
        node, boot, seq = m["node_id"], m["boot_id"], m["seq"]
        payload = json.dumps(m, sort_keys=True, separators=(",", ":"))
        with self.db:
            prior = self.db.execute(
                "SELECT payload,received_at FROM events WHERE node=? AND boot=? AND seq=?",
                (node, boot, seq),
            ).fetchone()
            if prior and self._identity_payload(
                json.loads(prior[0])
            ) != self._identity_payload(m):
                raise ValueError("event identity reused with different content")
            if m["type"] in RELIABLE:
                self.db.execute(
                    "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?)",
                    (node, boot, seq, payload, now),
                )
            generation = self._generation(m)
            if generation and m["type"] not in {"COMMAND_RECEIPT", "READY"}:
                self.db.execute(
                    """INSERT INTO current_state VALUES (?,?,?,?) ON CONFLICT(node) DO UPDATE SET
                    generation=excluded.generation,seq=excluded.seq,payload=excluded.payload
                    WHERE excluded.generation>current_state.generation OR
                    (excluded.generation=current_state.generation AND excluded.seq>current_state.seq)""",
                    (node, generation, seq, payload),
                )
            if m["type"] == "COMMAND_RESULT":
                status = (
                    "applied"
                    if m["status"] in {"applied", "already_applied"}
                    else "rejected"
                )
                self.db.execute(
                    """UPDATE commands SET result=?,status=CASE WHEN status IN
                    ('expired','applied','rejected') THEN status ELSE ? END WHERE id=? AND node=?""",
                    (payload, status, m["command_id"], node),
                )
            if m["type"] == "COMMAND_RECEIPT" and m["status"] == "received":
                self.db.execute(
                    "UPDATE commands SET status='delivered' WHERE id=? AND node=? AND status='queued'",
                    (m["command_id"], node),
                )
        return not bool(prior), prior["received_at"] if prior else now

    def queue(self, node, command_id, config, expires, now):
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

    def pending(self, now):
        """Commit command expiry and return the oldest live command for each node."""
        with self.db:
            self.db.execute(
                "UPDATE commands SET status='expired' WHERE expires<=? AND status IN ('queued','delivered')",
                (now,),
            )
        return self.db.execute("""SELECT * FROM commands c WHERE status IN ('queued','delivered') AND
            rowid=(SELECT MIN(rowid) FROM commands WHERE node=c.node AND status IN ('queued','delivered'))
            ORDER BY rowid""").fetchall()

    def listing(self, now):
        """Return structured command rows after persisting any newly expired statuses."""
        self.pending(now)
        return [
            dict(row)
            for row in self.db.execute("SELECT * FROM commands ORDER BY rowid")
        ]


class Bridge:
    """Serial-independent protocol orchestration; availability and ACK queues stay in RAM."""

    def __init__(self, store, mapping):
        """Bind durable storage to explicit node identities and allocate a bridge boot identity."""
        self.store, self.mapping = store, mapping
        self.boot = f"{secrets.randbits(64) or 1:016x}"
        self.seq = 0
        self.acks = {}
        self.available = {}
        self.last_command = {}
        # Keep per-boot watermarks across serial reconnects within this bridge process.
        self.last_telemetry = {}
        self.radio_pending = None
        self.radio_at = 0
        self.request = secrets.randbelow(UINT32_MAX)
        self.hello_request = None

    def reset(self):
        """Discard connection-local framing work and availability before serial reconnect."""
        self.acks.clear()
        self.available.clear()
        self.radio_pending = None
        self.last_command.clear()
        self.request = self.request % UINT32_MAX + 1
        self.hello_request = self.request
        return {"kind": "hello", "request_id": self.hello_request}

    def control(self, node, kind, **fields):
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

    def receive(self, envelope, now, monotonic=None):
        """Validate and durably accept an envelope before making a reliable ACK eligible to send."""
        lease_now = now if monotonic is None else monotonic
        if envelope.get("kind") != "telemetry":
            if (
                envelope.get("kind") == "hello"
                and envelope.get("request_id") == self.hello_request
            ):
                self.hello_request = None
            if (
                envelope.get("kind") in {"tx_result", "error"}
                and envelope.get("request_id") == self.radio_pending
            ):
                self.radio_pending = None
            return envelope
        m = validate(envelope, self.mapping)
        if m["type"] == "READY" and self.hello_request is not None:
            return dict(kind="ignored", reason="awaiting_fresh_connection", message=m)
        fresh, received_at = self.store.accept(m, now)
        identity = (m["node_id"], m["boot_id"])
        newer = m["seq"] > self.last_telemetry.get(identity, 0)
        # Best-effort batches and loss reports can be sent ahead of an older queued READY;
        # only state-changing reliable events and READY itself invalidate an older lease.
        if newer and (m["type"] in RELIABLE or m["type"] == "READY"):
            self.last_telemetry[identity] = m["seq"]
        if m["type"] == "COMMAND_RESULT" and m["status"] in {
            "applied",
            "already_applied",
        }:
            self.available.pop(m["node_id"], None)
        if m["type"] in RELIABLE:
            key = (m["node_id"], m["boot_id"], m["seq"])
            if len(self.acks) < MAX_PENDING_ACKS or key in self.acks:
                self.acks[key] = self.control(
                    m["node_id"],
                    "EVENT_ACK",
                    ack_boot_id=m["boot_id"],
                    ack_seq=m["seq"],
                )
        if m["type"] != "COMMAND_RECEIPT":
            if not m["occupied"]:
                self.available.pop(m["node_id"], None)
            elif m["type"] == "READY" and newer:
                current = self.store.db.execute(
                    "SELECT generation FROM current_state WHERE node=?", (m["node_id"],)
                ).fetchone()
                known = self.store.db.execute(
                    "SELECT generation FROM boots WHERE node=? AND boot=?", identity
                ).fetchone()
                if not current or (known and known[0] == current[0]):
                    remaining = m.get("listen_remaining_ms")
                    self.available[m["node_id"]] = (
                        float("inf")
                        if remaining is None
                        else lease_now + remaining / 1000
                    )
        return dict(
            kind="accepted", duplicate=not fresh, received_at=received_at, message=m
        )

    def next(self, now, monotonic=None):
        """Return one ACK or live command when the radio slot is free; expired commands never dispatch."""
        clock = now if monotonic is None else monotonic
        # Mark expired commands even while serial/radio is unavailable.
        pending = self.store.pending(now)
        if self.radio_pending is not None and clock - self.radio_at < RADIO_TIMEOUT_S:
            return None
        message = None
        if self.acks:
            _, control = self.acks.popitem()
            message = dict(kind="event_ack", message=control)
        else:
            for command in pending:
                node = command["node"]
                if (
                    self.available.get(node, 0) > clock
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
                    self.last_command[command["id"]] = clock
                    break
        if message:
            self.request = self.request % UINT32_MAX + 1
            message["request_id"] = self.request
            self.radio_pending = self.request
            self.radio_at = clock
        return message


def main(argv=None):
    """Run the bridge, enqueue configuration, or list command facts from the explicit database."""
    from transport import BAUD_RATE, Lines, write_message

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["protocol", "config", "commands"])
    parser.add_argument("--db", required=True)
    parser.add_argument(
        "--nodes", required=True, help="JSON file mapping node IDs to station MACs"
    )
    parser.add_argument("--port")
    parser.add_argument("--node")
    parser.add_argument("--command-id")
    parser.add_argument("--revision", type=int)
    parser.add_argument("--heartbeat-s", type=int, default=900)
    parser.add_argument("--movement-batch-s", type=int, default=3)
    parser.add_argument("--movement-enabled", choices=["true", "false"], default="true")
    parser.add_argument(
        "--radio-windows-enabled", choices=["true", "false"], default="true"
    )
    parser.add_argument("--radio-interval-s", type=int, default=30)
    parser.add_argument("--radio-open-s", type=int, default=5)
    parser.add_argument("--radio-motion-tail-s", type=int, default=10)
    parser.add_argument("--expires-at", type=float, help="UTC Unix seconds")
    args = parser.parse_args(argv)
    try:
        mapping = mapping_file(args.nodes)
        store = Store(args.db)
        if args.action == "commands":
            for row in store.listing(time.time()):
                print(json.dumps(row), flush=True)
            return 0
        if args.action == "config":
            if (
                args.node not in mapping
                or args.revision is None
                or args.expires_at is None
            ):
                parser.error(
                    "config requires mapped --node, --revision and --expires-at"
                )
            config = dict(
                revision=args.revision,
                heartbeat_s=args.heartbeat_s,
                movement_batch_s=args.movement_batch_s,
                movement_enabled=args.movement_enabled == "true",
                radio_windows_enabled=args.radio_windows_enabled == "true",
                radio_interval_s=args.radio_interval_s,
                radio_open_s=args.radio_open_s,
                radio_motion_tail_s=args.radio_motion_tail_s,
            )
            command_id = args.command_id or str(uuid.uuid4())
            store.queue(args.node, command_id, config, args.expires_at, time.time())
            print(json.dumps(dict(kind="queued", command_id=command_id)), flush=True)
            return 0
        if not args.port:
            parser.error("protocol requires an explicit --port")
        bridge = Bridge(store, mapping)
        while True:
            hello = bridge.reset()
            lines = Lines()
            try:
                with serial.Serial(
                    args.port,
                    BAUD_RATE,
                    timeout=SERIAL_POLL_S,
                    write_timeout=SERIAL_WRITE_TIMEOUT_S,
                    exclusive=True,
                ) as port:
                    write_message(port, hello)
                    hello_at = time.monotonic()
                    while True:
                        chunk = port.read(min(8192, max(1, port.in_waiting)))
                        for line in lines.feed(chunk):
                            try:
                                result = bridge.receive(
                                    json.loads(line), time.time(), time.monotonic()
                                )
                                print(json.dumps(result, ensure_ascii=True), flush=True)
                            except (
                                ValueError,
                                TypeError,
                                AttributeError,
                                sqlite3.Error,
                            ) as error:
                                print(
                                    json.dumps(dict(kind="error", reason=str(error))),
                                    flush=True,
                                )
                        try:
                            outgoing = bridge.next(time.time(), time.monotonic())
                        except sqlite3.Error as error:
                            print(
                                json.dumps(
                                    {"kind": "storage_error", "reason": str(error)}
                                ),
                                flush=True,
                            )
                            time.sleep(RETRY_DELAY_S)
                            continue
                        if (
                            bridge.hello_request is not None
                            and time.monotonic() - hello_at >= 1
                        ):
                            write_message(port, hello)
                            hello_at = time.monotonic()
                        if outgoing:
                            write_message(port, outgoing)
            except (serial.SerialException, OSError) as error:
                print(
                    json.dumps(dict(kind="disconnected", reason=str(error))), flush=True
                )
                time.sleep(RETRY_DELAY_S)
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.error(str(error))
