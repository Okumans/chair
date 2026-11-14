#!/usr/bin/env python3
"""Reference backend: durable ingestion and MQTT_ACK, never a Pi/ESP command executor."""

import argparse
import hashlib
import json
import os
import random
import sqlite3
import time
from collections import deque
from pathlib import Path

from contract import IDENTIFIER, ContractError, validate

APPLICATION_ID = 0x4D515431


def compact(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


class Ledger:
    """Commit delivery, semantic fact and outgoing receipt together before native ACK."""

    def __init__(self, path, site, consumer, bridges):
        if str(path) in {"", ":memory:"}:
            raise ValueError("use a dedicated durable example database")
        if (
            not all(IDENTIFIER.fullmatch(x) for x in (site, consumer, *bridges))
            or not bridges
        ):
            raise ValueError("invalid site/consumer/bridge allowlist")
        self.site, self.consumer, self.bridges = site, consumer, set(bridges)
        self.db = sqlite3.connect(path, timeout=0.2)
        existing = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
        ).fetchone()
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        application = self.db.execute("PRAGMA application_id").fetchone()[0]
        if (existing or version or application) and (
            version != 1 or application != APPLICATION_ID
        ):
            self.db.close()
            raise ValueError("not a compatible backend-example database")
        if self.db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
            self.db.close()
            raise ValueError("durable WAL unavailable")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS facts (
                semantic_key TEXT PRIMARY KEY, content_hash TEXT NOT NULL, content BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS deliveries (
                id TEXT PRIMARY KEY, topic TEXT NOT NULL, payload_hash TEXT NOT NULL,
                payload BLOB NOT NULL, semantic_key TEXT NOT NULL REFERENCES facts(semantic_key));
            CREATE TABLE IF NOT EXISTS receipts (
                id TEXT PRIMARY KEY REFERENCES deliveries(id), topic TEXT NOT NULL, payload BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS rejected (
                digest TEXT PRIMARY KEY, topic TEXT NOT NULL, reason TEXT NOT NULL);
        """)
        identity = compact(
            dict(site=site, consumer=consumer, bridges=sorted(bridges))
        ).decode()
        previous = self.db.execute(
            "SELECT value FROM metadata WHERE key='identity'"
        ).fetchone()
        if previous and previous[0] != identity:
            self.db.close()
            raise ValueError("database belongs to another backend identity")
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('identity',?)", (identity,)
            )
            self.db.execute(f"PRAGMA application_id={APPLICATION_ID}")
            self.db.execute("PRAGMA user_version=1")

    def ingest(self, topic, payload, *, qos=1, retain=False):
        target, value = validate(
            topic, payload, qos=qos, retain=retain, expected_site=self.site
        )
        if target.kind not in {"event", "report_update"}:
            raise ContractError("backend accepts only durable event/report streams")
        if value["bridge_id"] not in self.bridges:
            raise ContractError("bridge is not allowlisted")
        publication = value["publication_id"]
        if target.kind == "event":
            message = value["message"]
            key = [
                target.site,
                target.node,
                value["node_epoch"],
                "event",
                message["boot_id"],
                message["seq"],
            ]
            content = compact(message)
        else:
            key = [
                target.site,
                target.node,
                value["node_epoch"],
                "report",
                value["command_id"],
                value["report_seq"],
            ]
            content = compact({k: v for k, v in value.items() if k != "publication_id"})
        semantic = compact(key).decode()
        digest = hashlib.sha256(payload).hexdigest()
        content_hash = hashlib.sha256(content).hexdigest()
        receipt_topic = (
            f"smartchair/v1/{self.site}/bridges/{value['bridge_id']}/receipts"
        )
        receipt = compact(
            dict(
                schema_version=1,
                type="MQTT_ACK",
                consumer_id=self.consumer,
                publication_id=publication,
            )
        )
        with self.db:
            prior = self.db.execute(
                "SELECT topic,payload_hash FROM deliveries WHERE id=?", (publication,)
            ).fetchone()
            if prior and prior != (topic, digest):
                raise ContractError("publication ID reused with different topic/bytes")
            fact = self.db.execute(
                "SELECT content_hash FROM facts WHERE semantic_key=?", (semantic,)
            ).fetchone()
            if fact and fact[0] != content_hash:
                raise ContractError("semantic identity reused with different content")
            self.db.execute(
                "INSERT OR IGNORE INTO facts VALUES (?,?,?)",
                (semantic, content_hash, content),
            )
            self.db.execute(
                "INSERT OR IGNORE INTO deliveries VALUES (?,?,?,?,?)",
                (publication, topic, digest, payload, semantic),
            )
            # A duplicate requeues the same receipt even if an earlier receipt got PUBACK.
            self.db.execute(
                "INSERT OR IGNORE INTO receipts VALUES (?,?,?)",
                (publication, receipt_topic, receipt),
            )
        return receipt_topic, receipt

    def reject(self, topic, payload, reason):
        digest = hashlib.sha256(topic.encode() + b"\0" + payload).hexdigest()
        with self.db:
            exists = self.db.execute(
                "SELECT 1 FROM rejected WHERE digest=?", (digest,)
            ).fetchone()
            count = self.db.execute("SELECT COUNT(*) FROM rejected").fetchone()[0]
            if not exists and count >= 1024:
                raise sqlite3.OperationalError(
                    "rejection journal full; operator intervention needed"
                )
            self.db.execute(
                "INSERT OR IGNORE INTO rejected VALUES (?,?,?)", (digest, topic, reason)
            )

    def pending(self):
        return self.db.execute(
            "SELECT id,topic,payload FROM receipts ORDER BY rowid LIMIT 16"
        ).fetchall()

    def confirmed(self, publication):
        with self.db:
            self.db.execute("DELETE FROM receipts WHERE id=?", (publication,))

    def close(self):
        self.db.close()


class Backend:
    """Single-thread Paho loop: callbacks enqueue bounded work; the worker commits it."""

    def __init__(self, ledger, mqtt, client):
        self.ledger, self.mqtt, self.client = ledger, mqtt, client
        self.generation = 0
        self.connected = False
        self.ready = False
        self.subscriptions = set()
        self.subscription_deadline = 0
        self.work = deque()
        self.completed = deque()
        self.flights = {}  # Same live client's native MIDs remain owned across reconnects.
        self.recovery = False
        self.fatal = None
        self.publish_properties = mqtt.Properties(mqtt.PacketTypes.PUBLISH)
        self.publish_properties.PayloadFormatIndicator = 1
        self.publish_properties.ContentType = "application/json"
        client.on_connect = self.on_connect
        client.on_disconnect = self.on_disconnect
        client.on_subscribe = self.on_subscribe
        client.on_publish = self.on_publish
        client.on_message = self.on_message

    def on_connect(self, client, userdata, flags, reason, properties):
        if reason.is_failure:
            self.fatal = "broker rejected backend connection"
            return
        if self.generation == 0 and flags.session_present:
            self.fatal = "fresh client unexpectedly resumed a lost protocol session"
            return
        self.generation += 1
        self.connected, self.ready = True, False
        self.subscriptions.clear()
        now = time.monotonic()
        self.subscription_deadline = now + 30
        for mid, (publication, _) in list(self.flights.items()):
            self.flights[mid] = (publication, now + 30)
        for suffix in ("events", "commands/+/updates"):
            topic = f"smartchair/v1/{self.ledger.site}/nodes/+/{suffix}"
            options = self.mqtt.SubscribeOptions(
                qos=1, retainAsPublished=True, retainHandling=2
            )
            result, mid = client.subscribe(topic, options=options)
            if result != self.mqtt.MQTT_ERR_SUCCESS:
                self.fatal = "could not submit backend subscription"
                return
            self.subscriptions.add(mid)

    def on_disconnect(self, client, userdata, flags, reason, properties):
        self.connected, self.ready = False, False
        self.work.clear()  # No ACK for old delivery tokens; Pi/backend outboxes recover them.

    def on_subscribe(self, client, userdata, mid, reasons, properties):
        if (
            mid not in self.subscriptions
            or not reasons
            or any(x.is_failure for x in reasons)
        ):
            self.fatal = "backend subscription rejected or unmatched"
            return
        self.subscriptions.remove(mid)
        self.ready = not self.subscriptions

    def on_publish(self, client, userdata, mid, reason, properties):
        flight = self.flights.pop(mid, None)
        if flight is None:
            return
        self.completed.append((flight[0], reason.is_failure))

    def on_message(self, client, userdata, message):
        if len(self.work) >= 64 or len(message.payload) > 16384:
            self.recovery = True
            return
        self.work.append(
            (
                self.generation,
                message.mid,
                message.qos,
                message.topic,
                bytes(message.payload),
                bool(message.retain),
            )
        )

    def step(self, now=None):
        now = time.monotonic() if now is None else now
        while self.completed:
            publication, failure = self.completed[0]
            if failure:
                self.fatal = (
                    "receipt publication rejected; inspect broker policy before restart"
                )
                self.completed.popleft()
                return
            self.ledger.confirmed(publication)
            self.completed.popleft()
        if not self.connected:
            return
        for _ in range(min(8, len(self.work))):
            generation, mid, qos, topic, payload, retain = self.work.popleft()
            if generation != self.generation:
                continue
            try:
                self.ledger.ingest(topic, payload, qos=qos, retain=retain)
            except ContractError as error:
                # Native ACK only after the safe rejection journal commits; never MQTT_ACK invalid data.
                self.ledger.reject(topic, payload, str(error))
            if self.client.ack(mid, qos) != self.mqtt.MQTT_ERR_SUCCESS:
                self.recovery = True
                return
        if not self.ready and now >= self.subscription_deadline:
            self.recovery = True
        if any(now >= deadline for _, deadline in self.flights.values()):
            self.recovery = True
        if not self.ready or self.recovery or self.fatal:
            return
        active = {publication for publication, _ in self.flights.values()}
        for publication, topic, payload in self.ledger.pending():
            if publication in active or len(self.flights) >= 16:
                continue
            info = self.client.publish(
                topic, payload, qos=1, retain=False, properties=self.publish_properties
            )
            if info.rc != self.mqtt.MQTT_ERR_SUCCESS:
                self.recovery = True
                return
            # client.loop() and callbacks only run in this thread, after registration.
            self.flights[info.mid] = (publication, now + 30)
            active.add(publication)


def main(argv=None):
    import paho.mqtt.client as mqtt

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broker", required=True)
    parser.add_argument("--port", type=int, default=8883)
    parser.add_argument("--site", required=True)
    parser.add_argument("--consumer", required=True)
    parser.add_argument("--bridge", action="append", required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--ca-file")
    parser.add_argument("--allow-local-plaintext", action="store_true")
    args = parser.parse_args(argv)
    if args.allow_local_plaintext and args.broker not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        parser.error("plaintext example is restricted to loopback")
    ledger = Ledger(args.db, args.site, args.consumer, args.bridge)
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"smartchair-backend-{args.site}-{args.consumer}",
        protocol=mqtt.MQTTv5,
        manual_ack=True,
    )
    if not args.allow_local_plaintext:
        client.tls_set(ca_certs=args.ca_file)
    if os.getenv("MQTT_USERNAME"):
        client.username_pw_set(os.environ["MQTT_USERNAME"], os.getenv("MQTT_PASSWORD"))
    client.max_inflight_messages_set(16)
    client.max_queued_messages_set(16)
    backend = Backend(ledger, mqtt, client)
    properties = mqtt.Properties(mqtt.PacketTypes.CONNECT)
    properties.SessionExpiryInterval = 86400
    properties.ReceiveMaximum = 16
    properties.MaximumPacketSize = 65536
    first, connecting, deadline, retry_at, delay = True, False, 0, 0, 1
    try:
        while True:
            now = time.monotonic()
            if backend.fatal:
                raise RuntimeError(backend.fatal)
            if not backend.connected and not connecting and now >= retry_at:
                try:
                    if first:
                        client.connect(
                            args.broker,
                            args.port,
                            keepalive=30,
                            clean_start=mqtt.MQTT_CLEAN_START_FIRST_ONLY,
                            properties=properties,
                        )
                        first = False
                    else:
                        client.reconnect()
                    connecting, deadline = True, now + 30
                except OSError:
                    retry_at = now + random.uniform(delay, delay * 1.2)
                    delay = min(30, delay * 2)
            result = client.loop(timeout=0.05)
            if backend.connected:
                connecting, delay = False, 1
            try:
                backend.step()
            except sqlite3.Error:
                backend.recovery = (
                    True  # No incoming ACK was emitted before a failed commit.
                )
            if (
                backend.recovery
                or (connecting and now >= deadline)
                or result != mqtt.MQTT_ERR_SUCCESS
            ):
                client.disconnect()
                backend.connected = backend.ready = False
                backend.work.clear()
                backend.recovery, connecting = False, False
                if retry_at <= now:
                    retry_at = now + random.uniform(delay, delay * 1.2)
                    delay = min(30, delay * 2)
            if not backend.connected:
                time.sleep(0.05)
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError) as error:
        parser.exit(1, str(error) + "\n")
    finally:
        client.disconnect()
        ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
