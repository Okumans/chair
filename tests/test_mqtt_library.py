import itertools
import json
import os
import pty
import re
import select
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import paho.mqtt.client as mqtt

from chair_mqtt import (
    BridgeConfig,
    ChairMqttBridge,
    ConfigRequest,
    ControllerLedger,
    MovementBatch,
    StateSync,
)
from chair_mqtt.backend import BackendLedger
from chair_mqtt.contract import ContractError, validate
from chair_mqtt.coordinator import Coordinator, Oversized
from chair_mqtt.protocol_bridge import Bridge, Store
from chair_mqtt.store import MQTT_SCHEMA_VERSION, CommandAdmissionError, MqttStore

NODE = "chair-01"
MAC = "02:00:00:00:00:01"
MAPPING = {NODE: MAC}


def event(kind="STATE_SYNC", seq=1, **fields):
    message = dict(
        version=2,
        type=kind,
        node_id=NODE,
        boot_id="0000000000000001",
        seq=seq,
        uptime_ms=100,
        occupied=True,
        battery_mv=None,
        config_revision=0,
        health_flags=2,
    )
    if kind == "STATE_SYNC":
        message["boot_generation"] = "0000000000000001"
    message.update(fields)
    return message


def envelope(message):
    return dict(kind="telemetry", source_mac=MAC, message=message)


class FakeMqttClient:
    def __init__(self):
        self.next_mid = 0
        self.subscriptions, self.published, self.acks = [], [], []

    def subscribe(self, topic, **kwargs):
        self.next_mid += 1
        self.subscriptions.append((topic, kwargs, self.next_mid))
        return 0, self.next_mid

    def publish(self, topic, **kwargs):
        self.next_mid += 1
        self.published.append((topic, kwargs, self.next_mid))
        return SimpleNamespace(rc=0, mid=self.next_mid)

    def ack(self, mid, qos):
        self.acks.append((mid, qos))
        return 0


class MqttContractTests(unittest.TestCase):
    def test_examples_match_specification(self):
        text = Path("docs/MQTT_SPECS.md").read_text()
        blocks = re.findall(r"```json\s*\n(.*?)\n```", text, re.S)
        routes = {
            "STATE_SYNC": ("nodes/chair-01/events", 1, False),
            "MOVEMENT_BATCH": ("nodes/chair-01/movement", 0, False),
            "CONFIG_SET": ("nodes/chair-01/commands/config", 1, False),
            "COMMAND_STATUS": (
                "nodes/chair-01/commands/37c7984c-33eb-4dcc-93be-e2b11bb2436d/updates",
                1,
                False,
            ),
            "MQTT_ACK": ("bridges/pi-01/receipts", 1, False),
            "BRIDGE_STATUS": ("bridges/pi-01/status", 1, True),
            "ERROR": ("nodes/chair-01/command_errors", 1, False),
        }
        self.assertEqual(len(blocks), 7)
        for block in blocks:
            data = json.loads(block)
            kind = data.get("type") or data.get("message", {}).get("type") or "ERROR"
            topic, qos, retain = routes[kind]
            with self.subTest(kind=kind):
                validate(
                    "smartchair/v1/" + topic, block.encode(), qos=qos, retain=retain
                )

    def test_reject_duplicate_json_key_and_incorrect_route(self):
        with self.assertRaises(ContractError):
            validate(
                "smartchair/v1/bridges/pi-01/receipts",
                b'{"type":"MQTT_ACK","type":"MQTT_ACK"}',
                qos=1,
                retain=False,
            )
        with self.assertRaises(ContractError):
            validate(
                "smartchair/v1/lab/bridges/pi-01/receipts", b"{}", qos=1, retain=False
            )

    def test_pydantic_movement_preserves_raw_samples_and_rejects_bad_offsets(self):
        data = event(
            "MOVEMENT_BATCH",
            seq=2,
            batch_start_ms=0,
            duration_ms=40,
            end_reason="stopped",
            samples=[dict(offset_ms=0, ax=-32768, ay=2, az=3, gx=4, gy=5, gz=6)],
        )
        batch = MovementBatch.model_validate(data)
        self.assertEqual(batch.samples[0].ax, -32768)
        data["samples"][0]["offset_ms"] = 41
        with self.assertRaises(ValueError):
            MovementBatch.model_validate(data)


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "chair.sqlite"

    def store(self):
        value = MqttStore(
            self.path,
            bridge_id="pi-01",
            consumer_id="backend-01",
            mapping=MAPPING,
            clock_trusted=lambda: True,
        )
        self.addCleanup(value.db.close)
        return value

    def command(self, command_id="37c7984c-33eb-4dcc-93be-e2b11bb2436d"):
        value = {
            "schema_version": 1,
            "expires_at_ms": 1800000060000,
            "message": {
                "version": 2,
                "type": "CONFIG_SET",
                "node_id": NODE,
                "command_id": command_id,
                "revision": 2,
                "heartbeat_s": 900,
                "movement_batch_s": 3,
                "movement_enabled": True,
                "radio_windows_enabled": True,
                "radio_interval_s": 30,
                "radio_open_s": 5,
                "radio_motion_tail_s": 10,
            },
        }
        return json.dumps(value, separators=(",", ":")).encode()


class MqttStoreTests(StoreFixture):
    def test_command_admission_duplicate_conflict_and_terminal_result(self):
        store = self.store()
        store.accept(event(), 1800000000)
        topic = "smartchair/v1/nodes/chair-01/commands/config"
        payload = self.command()
        self.assertEqual(store.admit_command(topic, payload, now=1800000000), "queued")
        self.assertEqual(
            store.admit_command(topic, payload, now=1800000061), "duplicate"
        )
        changed = json.loads(payload)
        changed["message"]["revision"] += 1
        with self.assertRaisesRegex(CommandAdmissionError, "command_id_conflict"):
            store.admit_command(topic, json.dumps(changed).encode(), now=1800000000)
        command_id = changed["message"]["command_id"]
        receipt = dict(
            version=2,
            type="COMMAND_RECEIPT",
            node_id=NODE,
            boot_id="0000000000000001",
            seq=2,
            command_id=command_id,
            status="received",
        )
        store.accept(receipt, 1800000001)
        result = event("COMMAND_RESULT", seq=3, command_id=command_id, status="applied")
        store.accept(result, 1800000002)
        row = store.db.execute(
            "SELECT report,reserved FROM mqtt_commands WHERE id=?", (command_id,)
        ).fetchone()
        report = json.loads(row["report"])
        self.assertEqual(
            (report["status"], report["report_seq"], row["reserved"]), ("applied", 3, 0)
        )
        self.assertEqual(report["message"], result)
        self.assertEqual(
            store.db.execute("SELECT count(*) FROM mqtt_outbox").fetchone()[0], 5
        )

    def test_expired_command_late_result_keeps_expired_status(self):
        store = self.store()
        store.accept(event(), 1800000000)
        topic = "smartchair/v1/nodes/chair-01/commands/config"
        command_id = "37c7984c-33eb-4dcc-93be-e2b11bb2436d"
        store.admit_command(topic, self.command(), now=1800000000)
        store.pending(1800000061)
        result = event("COMMAND_RESULT", seq=2, command_id=command_id, status="applied")
        store.accept(result, 1800000062)
        report = json.loads(
            store.db.execute(
                "SELECT report FROM mqtt_commands WHERE id=?", (command_id,)
            ).fetchone()[0]
        )
        self.assertEqual((report["status"], report["late_result"]), ("expired", True))
        self.assertEqual(report["message"], result)

    def test_result_received_after_deadline_expires_before_applying(self):
        store = self.store()
        store.accept(event(), 1800000000)
        topic = "smartchair/v1/nodes/chair-01/commands/config"
        command_id = "37c7984c-33eb-4dcc-93be-e2b11bb2436d"
        store.admit_command(topic, self.command(), now=1800000000)
        result = event("COMMAND_RESULT", seq=2, command_id=command_id, status="applied")
        # No explicit expiry sweep occurs between the deadline and the result.
        store.accept(result, 1800000061)
        report = json.loads(
            store.db.execute(
                "SELECT report FROM mqtt_commands WHERE id=?", (command_id,)
            ).fetchone()[0]
        )
        self.assertEqual((report["status"], report["late_result"]), ("expired", True))

    def test_backward_wall_clock_does_not_extend_command_dispatch(self):
        mono = [10.0]
        store = MqttStore(
            self.path,
            bridge_id="pi-01",
            consumer_id="backend-01",
            mapping=MAPPING,
            clock_trusted=lambda: True,
            monotonic_clock=lambda: mono[0],
        )
        self.addCleanup(store.db.close)
        topic = "smartchair/v1/nodes/chair-01/commands/config"
        store.admit_command(topic, self.command(), now=1800000000)
        mono[0] = 71.0
        self.assertEqual(store.pending(1799999900), [])
        report = json.loads(
            store.db.execute("SELECT report FROM mqtt_commands").fetchone()[0]
        )
        self.assertEqual(report["status"], "expired")

    def test_command_deadline_rechecked_after_database_lock(self):
        store = MqttStore(
            self.path,
            bridge_id="pi-01",
            consumer_id="backend-01",
            mapping=MAPPING,
            clock_trusted=lambda: True,
            wall_clock=lambda: 1800000061,
        )
        self.addCleanup(store.db.close)
        topic = "smartchair/v1/nodes/chair-01/commands/config"
        self.assertEqual(
            store.admit_command(topic, self.command(), now=1800000000), "expired"
        )
        self.assertEqual(store.pending(1800000061), [])

    def test_untrusted_clock_blocks_dispatch_but_not_duplicate_admission(self):
        trusted = [True]
        store = MqttStore(
            self.path,
            bridge_id="pi-01",
            consumer_id="backend-01",
            mapping=MAPPING,
            clock_trusted=lambda: trusted[0],
        )
        self.addCleanup(store.db.close)
        topic = "smartchair/v1/nodes/chair-01/commands/config"
        store.admit_command(topic, self.command(), now=1800000000)
        trusted[0] = False
        self.assertEqual(store.pending(1800000001), [])
        self.assertEqual(
            store.admit_command(topic, self.command(), now=1800000001), "duplicate"
        )

    def test_expired_status_cleanup_and_duplicate_response_are_durable(self):
        store = self.store()
        topic = "smartchair/v1/nodes/chair-01/commands/config"
        command_id = "37c7984c-33eb-4dcc-93be-e2b11bb2436d"
        self.assertEqual(
            store.admit_command(topic, self.command(), now=1800000061), "expired"
        )
        self.assertEqual(store.schedule_retention(1800000061 + 7 * 86400 - 1), 0)
        self.assertEqual(store.schedule_retention(1800000061 + 7 * 86400), 1)
        projection = store.db.execute(
            "SELECT desired FROM mqtt_projection WHERE topic LIKE '%/status'"
        ).fetchone()[0]
        self.assertEqual(projection, b"")
        self.assertEqual(
            store.admit_command(topic, self.command(), now=1801000000), "duplicate"
        )
        response = store.db.execute(
            "SELECT topic,payload FROM mqtt_responses WHERE command_id=?", (command_id,)
        ).fetchone()
        self.assertTrue(response["topic"].endswith("/status"))
        self.assertEqual(json.loads(response["payload"])["status"], "expired")
        self.assertEqual(
            store.db.execute(
                "SELECT retention_state FROM mqtt_commands WHERE id=?", (command_id,)
            ).fetchone()[0],
            "pending",
        )

    def test_capacity_cleanup_uses_terminal_order_even_if_clock_untrusted(self):
        trusted = [True]
        store = MqttStore(
            self.path,
            bridge_id="pi-01",
            consumer_id="backend-01",
            mapping=MAPPING,
            clock_trusted=lambda: trusted[0],
        )
        self.addCleanup(store.db.close)
        topic = "smartchair/v1/nodes/chair-01/commands/config"
        for number in range(1, 258):
            command_id = f"00000000-0000-0000-0000-{number:012x}"
            self.assertEqual(
                store.admit_command(topic, self.command(command_id), now=1800000061),
                "expired",
            )
        trusted[0] = False
        self.assertEqual(store.schedule_retention(1800000061), 1)
        oldest = store.db.execute(
            "SELECT id,retention_state FROM mqtt_commands ORDER BY terminal_order LIMIT 1"
        ).fetchone()
        self.assertEqual(
            (oldest["id"], oldest["retention_state"]),
            ("00000000-0000-0000-0000-000000000001", "pending"),
        )
        self.assertEqual(
            store.db.execute(
                "SELECT count(*) FROM mqtt_commands WHERE retention_state='active'"
            ).fetchone()[0],
            256,
        )

    def test_reliable_sensor_acceptance_commits_event_and_outbox_before_ack(self):
        store = self.store()
        bridge = Bridge(store, MAPPING)
        bridge.receive(envelope(event()), 1800000000)
        self.assertEqual(len(bridge.acks), 1)
        other = sqlite3.connect(self.path)
        self.addCleanup(other.close)
        self.assertEqual(other.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        row = other.execute("SELECT topic,payload FROM mqtt_outbox").fetchone()
        self.assertEqual(row[0], "smartchair/v1/nodes/chair-01/events")
        payload = json.loads(row[1])
        self.assertEqual(payload["message"], event())
        self.assertEqual(payload["received_at_ms"], 1800000000000)
        self.assertEqual(
            other.execute("SELECT count(*) FROM mqtt_projection").fetchone()[0], 1
        )
        bridge.receive(envelope(event()), 1800000002)
        self.assertEqual(
            other.execute("SELECT count(*) FROM mqtt_outbox").fetchone()[0], 1
        )

    def test_failed_outbox_write_rolls_back_fact_and_sensor_ack(self):
        store = self.store()
        store.db.executescript(
            "CREATE TRIGGER fail_mqtt BEFORE INSERT ON mqtt_outbox BEGIN SELECT RAISE(ABORT,'storage failure'); END;"
        )
        bridge = Bridge(store, MAPPING)
        with self.assertRaises(sqlite3.Error):
            bridge.receive(envelope(event()), 1800000000)
        self.assertEqual(
            store.db.execute("SELECT count(*) FROM events").fetchone()[0], 0
        )
        self.assertEqual(len(bridge.acks), 0)

    def test_full_projection_pool_rolls_back_event_and_outbox(self):
        store = self.store()
        bridge = Bridge(store, MAPPING)
        with (
            patch("chair_mqtt.store.MAX_PROJECTION_ROWS", 0),
            self.assertRaises(sqlite3.Error),
        ):
            bridge.receive(envelope(event()), 1800000000)
        self.assertEqual(
            store.db.execute("SELECT count(*) FROM events").fetchone()[0], 0
        )
        self.assertEqual(
            store.db.execute("SELECT count(*) FROM mqtt_outbox").fetchone()[0], 0
        )
        self.assertEqual(len(bridge.acks), 0)

    def test_active_command_retains_projection_capacity_until_expiry(self):
        store = self.store()
        topic = "smartchair/v1/nodes/chair-01/commands/config"
        with patch("chair_mqtt.store.MAX_PROJECTION_ROWS", 2):
            self.assertEqual(
                store.admit_command(topic, self.command(), now=1800000000), "queued"
            )
            store.db.execute("UPDATE mqtt_projection SET dirty=0")
            store._queue_projection("unrelated/topic", b"value")
            with self.assertRaises(sqlite3.OperationalError):
                store._queue_projection("another/topic", b"value")
            store.pending(1800000061)
        report = store.db.execute("SELECT report FROM mqtt_commands").fetchone()[0]
        self.assertEqual(json.loads(report)["status"], "expired")

    def test_low_free_disk_withholds_new_sensor_ack(self):
        store = self.store()
        bridge = Bridge(store, MAPPING)
        with (
            patch(
                "chair_mqtt.store.shutil.disk_usage",
                return_value=SimpleNamespace(free=0),
            ),
            self.assertRaises(sqlite3.Error),
        ):
            bridge.receive(envelope(event()), 1800000000)
        self.assertEqual(
            store.db.execute("SELECT count(*) FROM events").fetchone()[0], 0
        )
        self.assertEqual(len(bridge.acks), 0)

    def test_upgrade_backfills_events_and_protocol_only_binary_refuses_new_database(
        self,
    ):
        old = Store(self.path)
        old.accept(event(), 1800000000)
        old.db.close()
        store = self.store()
        self.assertEqual(
            store.db.execute("PRAGMA user_version").fetchone()[0], MQTT_SCHEMA_VERSION
        )
        self.assertEqual(
            store.db.execute("SELECT count(*) FROM mqtt_outbox").fetchone()[0], 1
        )
        self.assertEqual(
            store.db.execute("SELECT count(*) FROM events").fetchone()[0], 1
        )
        with self.assertRaises(ValueError):
            Store(self.path)

    def test_migration_omits_unrelated_legacy_nulls_but_keeps_battery_null(self):
        old = Store(self.path)
        legacy = event(ack_seq=None, reason=None)
        old.accept(legacy, 1800000000)
        old.db.close()
        store = self.store()
        published = json.loads(
            store.db.execute("SELECT payload FROM mqtt_outbox").fetchone()[0]
        )
        self.assertNotIn("ack_seq", published["message"])
        self.assertNotIn("reason", published["message"])
        self.assertIn("battery_mv", published["message"])
        self.assertIsNone(published["message"]["battery_mv"])
        self.assertIsNone(
            json.loads(
                store.db.execute(
                    "SELECT desired FROM mqtt_projection WHERE topic LIKE '%/state'"
                ).fetchone()[0]
            )["message"]["battery_mv"]
        )

    def test_failed_upgrade_preserves_old_database_and_version(self):
        old = Store(self.path)
        old.accept(event(node_id="other-node"), 1800000000)
        old.db.close()
        with self.assertRaises(ValueError):
            self.store()
        db = sqlite3.connect(self.path)
        self.addCleanup(db.close)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        self.assertIsNone(
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='mqtt_outbox'"
            ).fetchone()
        )

    def test_untrusted_first_receipt_time_stays_unknown_after_boot_is_known(self):
        trusted = [False]
        store = MqttStore(
            self.path,
            bridge_id="pi-01",
            consumer_id="backend-01",
            mapping=MAPPING,
            clock_trusted=lambda: trusted[0],
        )
        self.addCleanup(store.db.close)
        store.accept(event("HEARTBEAT", seq=2), 1800000000)
        trusted[0] = True
        store.accept(event(seq=1), 1800000001)
        payload = json.loads(
            store.db.execute(
                "SELECT desired FROM mqtt_projection WHERE topic LIKE '%/state'"
            ).fetchone()[0]
        )
        self.assertEqual(payload["message"]["type"], "HEARTBEAT")
        self.assertIsNone(payload["received_at_ms"])
        self.assertEqual(payload["boot_generation"], "0000000000000001")

    def test_best_effort_movement_is_bounded_and_has_no_durable_receipt(self):
        store = self.store()
        batch = event(
            "MOVEMENT_BATCH",
            seq=2,
            batch_start_ms=0,
            duration_ms=40,
            end_reason="stopped",
            samples=[dict(offset_ms=0, ax=1, ay=2, az=3, gx=4, gy=5, gz=6)],
        )
        for _ in range(130):
            store.accept(batch, 1800000000)
        self.assertEqual(len(store.best_effort), 128)
        self.assertEqual(store.best_effort_dropped, 2)
        self.assertEqual(
            store.db.execute("SELECT count(*) FROM mqtt_outbox").fetchone()[0], 0
        )
        self.assertEqual(
            json.loads(store.best_effort[0][1])["message"]["samples"][0]["gx"], 4
        )


class CoordinatorTests(StoreFixture):
    def coordinator(self):
        store = self.store()
        client = FakeMqttClient()
        worker = Coordinator(
            store, client, monotonic=lambda: 100.0, wall_clock=lambda: 1800000000
        )
        worker.on_connect(
            SimpleNamespace(session_present=False), SimpleNamespace(is_failure=False), 1
        )
        return store, client, worker

    def ready(self, worker, client):
        for _, _, mid in client.subscriptions:
            worker.on_subscribe(mid, [SimpleNamespace(is_failure=False, value=1)])
        self.assertTrue(worker.ready)

    def test_subscriptions_reject_qos_zero_and_do_not_claim_readiness(self):
        _, client, worker = self.coordinator()
        mid = client.subscriptions[0][2]
        worker.on_subscribe(mid, [SimpleNamespace(is_failure=False, value=0)])
        self.assertFalse(worker.ready)
        self.assertIsNotNone(worker.fatal)
        # A fatal policy failure survives reconnection without new subscriptions.
        count = len(client.subscriptions)
        worker.on_disconnect()
        worker.on_connect(
            SimpleNamespace(session_present=True), SimpleNamespace(is_failure=False), 2
        )
        self.assertEqual(len(client.subscriptions), count)

    def test_command_is_admitted_before_ack_and_duplicate_is_not_reexecuted(self):
        store, client, worker = self.coordinator()
        topic = "smartchair/v1/nodes/chair-01/commands/config"
        worker.on_message(1, 22, 1, False, topic, self.command(), 1800000000)
        self.assertEqual(client.acks, [])
        self.ready(worker, client)
        self.assertEqual(client.acks, [(22, 1)])
        self.assertEqual(
            store.db.execute("SELECT count(*) FROM commands").fetchone()[0], 1
        )
        worker.on_message(1, 23, 1, False, topic, self.command(), 1800000001)
        self.assertEqual(client.acks, [(22, 1), (23, 1)])
        self.assertEqual(
            store.db.execute("SELECT count(*) FROM commands").fetchone()[0], 1
        )

    def test_backend_receipt_completes_only_matching_consumer(self):
        store, client, worker = self.coordinator()
        self.ready(worker, client)
        store.accept(event(), 1800000000)
        worker.tick(1800000000)
        row = store.db.execute(
            "SELECT publication_id,state FROM mqtt_outbox"
        ).fetchone()
        self.assertEqual(row["state"], "inflight")
        receipt = {
            "schema_version": 1,
            "type": "MQTT_ACK",
            "consumer_id": "backend-01",
            "publication_id": row["publication_id"],
        }
        topic = "smartchair/v1/bridges/pi-01/receipts"
        worker.on_message(
            1, 50, 1, False, topic, json.dumps(receipt).encode(), 1800000001
        )
        self.assertEqual(
            store.db.execute("SELECT state FROM mqtt_outbox").fetchone()[0], "completed"
        )
        self.assertEqual(client.acks, [(50, 1)])
        mid = next(mid for t, _, mid in client.published if t.endswith("/events"))
        worker.on_publish(mid, SimpleNamespace(is_failure=False))
        self.assertEqual(
            store.db.execute("SELECT state FROM mqtt_outbox").fetchone()[0], "completed"
        )

    def test_storage_failure_withholds_incoming_ack(self):
        store, client, worker = self.coordinator()
        self.ready(worker, client)
        store.db.executescript(
            "CREATE TRIGGER fail_report BEFORE INSERT ON mqtt_outbox BEGIN SELECT RAISE(ABORT,'storage failure'); END;"
        )
        worker.on_message(
            1,
            22,
            1,
            False,
            "smartchair/v1/nodes/chair-01/commands/config",
            self.command(),
            1800000000,
        )
        self.assertEqual(client.acks, [])
        self.assertTrue(worker.reconnect_required)
        self.assertEqual(
            store.db.execute("SELECT count(*) FROM commands").fetchone()[0], 0
        )

    def test_negative_puback_quota_retries_but_permanent_failure_quarantines(self):
        store, client, worker = self.coordinator()
        self.ready(worker, client)
        store.accept(event(), 1800000000)
        worker.tick(1800000000)
        mid = next(mid for t, _, mid in client.published if t.endswith("/events"))
        for other in list(worker.flights):
            if other != mid:
                worker.on_publish(other, SimpleNamespace(is_failure=False))
        worker.on_publish(mid, SimpleNamespace(is_failure=True, value=0x97))
        row = store.db.execute(
            "SELECT publication_id,state FROM mqtt_outbox"
        ).fetchone()
        self.assertEqual(row["state"], "pending")
        retry_at = store.db.execute(
            "SELECT retry_at_ms FROM mqtt_outbox WHERE publication_id=?",
            (row["publication_id"],),
        ).fetchone()[0]
        self.assertGreater(retry_at, 100_000)  # Backed off past the current time.
        worker.monotonic = lambda: 1000.0
        worker.tick(1800000001)
        latest = [mid for t, _, mid in client.published if t.endswith("/events")][-1]
        worker.on_publish(latest, SimpleNamespace(is_failure=True, value=0x87))
        self.assertEqual(
            store.db.execute("SELECT state FROM mqtt_outbox").fetchone()[0],
            "quarantined",
        )

    def test_refused_connack_recovers_on_next_successful_connection(self):
        store = self.store()
        client = FakeMqttClient()
        worker = Coordinator(
            store, client, monotonic=lambda: 100.0, wall_clock=lambda: 1800000000
        )
        refused = SimpleNamespace(is_failure=True)
        worker.on_connect(SimpleNamespace(session_present=False), refused, 1)
        self.assertIsNone(worker.fatal)
        self.assertFalse(worker.connected)
        worker.on_connect(
            SimpleNamespace(session_present=False), SimpleNamespace(is_failure=False), 2
        )
        self.ready(worker, client)
        self.assertIsNone(worker.fatal)
        store.accept(event(), 1800000000)
        worker.tick(1800000000)
        self.assertTrue(any(t.endswith("/events") for t, _, _ in client.published))

    def test_failed_subscribe_submission_requests_reconnect_not_fatal(self):
        store = self.store()
        client = FakeMqttClient()
        results = iter([(mqtt.MQTT_ERR_NO_CONN, None)])
        original = client.subscribe
        client.subscribe = lambda topic, **kw: (
            next(results, None) or original(topic, **kw)
        )
        worker = Coordinator(
            store, client, monotonic=lambda: 100.0, wall_clock=lambda: 1800000000
        )
        connected = SimpleNamespace(is_failure=False)
        worker.on_connect(SimpleNamespace(session_present=False), connected, 1)
        self.assertIsNone(worker.fatal)
        self.assertTrue(worker.reconnect_required)
        worker.on_disconnect()
        worker.on_connect(SimpleNamespace(session_present=True), connected, 2)
        self.assertFalse(worker.reconnect_required)
        client.subscriptions = client.subscriptions[-2:]
        self.ready(worker, client)
        self.assertIsNone(worker.fatal)

    def test_qos1_publish_queued_while_disconnected_is_not_sent_twice(self):
        store, client, worker = self.coordinator()
        self.ready(worker, client)
        original = client.publish

        def queued_by_paho(topic, **kwargs):
            info = original(topic, **kwargs)
            return SimpleNamespace(rc=mqtt.MQTT_ERR_NO_CONN, mid=info.mid)

        client.publish = queued_by_paho
        store.accept(event(), 1800000000)
        worker.tick(1800000000)
        worker.tick(1800000001)
        sent = [mid for t, _, mid in client.published if t.endswith("/events")]
        self.assertEqual(len(sent), 1)
        worker.on_publish(sent[0], SimpleNamespace(is_failure=False))
        self.assertEqual(
            store.db.execute("SELECT state FROM mqtt_outbox").fetchone()[0], "waiting"
        )

    def test_puback_deadline_does_not_expire_while_disconnected(self):
        store, client, worker = self.coordinator()
        self.ready(worker, client)
        store.accept(event(), 1800000000)
        worker.tick(1800000000)
        self.assertTrue(worker.flights)
        worker.on_disconnect()
        worker.monotonic = lambda: 1000.0
        worker.tick(1800000900)
        self.assertFalse(worker.reconnect_required)

    def test_steady_worker_iteration_cost_does_not_grow_with_backlog(self):
        def steps(backlog):
            path = Path(self.tmp.name) / f"backlog-{backlog}.sqlite"
            store = MqttStore(
                path,
                bridge_id="pi-01",
                consumer_id="backend-01",
                mapping=MAPPING,
                clock_trusted=lambda: True,
            )
            self.addCleanup(store.db.close)
            client = FakeMqttClient()
            worker = Coordinator(
                store, client, monotonic=lambda: 100.0, wall_clock=lambda: 1800000000
            )
            worker.on_connect(
                SimpleNamespace(session_present=False),
                SimpleNamespace(is_failure=False),
                1,
            )
            self.ready(worker, client)
            # Published facts awaiting a backend receipt, plus confirmed history.
            with store.db:
                store.db.executemany(
                    "INSERT INTO mqtt_outbox(publication_id,logical_key,topic,payload,"
                    "consumer_id,state,retry_at_ms) VALUES (?,?,?,?,?,?,?)",
                    (
                        (f"id-{n}", f"key-{n}", "t", b"{}", "backend-01", state, 10**9)
                        for n in range(backlog)
                        for state in ("waiting",)
                    ),
                )
                store.db.executemany(
                    "INSERT INTO mqtt_outbox(publication_id,logical_key,topic,payload,"
                    "consumer_id,state) VALUES (?,?,?,?,?,'completed')",
                    (
                        (f"done-{n}", f"done-{n}", "t", b"{}", "backend-01")
                        for n in range(backlog)
                    ),
                )
            status = dict(gateway_connected=True, gateway_ready=True, now=1800000000)
            worker.bridge_status("instance", **status)
            worker.tick(1800000000)
            count = [0]
            store.db.set_progress_handler(
                lambda: count.__setitem__(0, count[0] + 1) or 0, 100
            )
            for _ in range(10):
                worker.bridge_status("instance", **status)
                worker.tick(1800000000)
            store.db.set_progress_handler(None, 0)
            return count[0]

        small, large = steps(20), steps(4000)
        self.assertLess(large, 2 * small + 20)

    def test_backed_off_outbox_rows_do_not_starve_later_rows(self):
        store, client, worker = self.coordinator()
        self.ready(worker, client)
        for seq in range(1, 21):
            store.accept(event("HEARTBEAT", seq=seq), 1800000000)
        original, refused = client.publish, [16]

        def queue_full_for_first_rows(topic, **kwargs):
            if topic.endswith("/events") and refused[0]:
                refused[0] -= 1
                return SimpleNamespace(rc=mqtt.MQTT_ERR_QUEUE_SIZE, mid=None)
            return original(topic, **kwargs)

        client.publish = queue_full_for_first_rows
        worker.tick(1800000000)
        worker.tick(1800000000)
        published = store.db.execute(
            "SELECT count(*) FROM mqtt_outbox WHERE state='inflight'"
        ).fetchone()[0]
        self.assertEqual(published, 4)

    def test_new_fact_cost_does_not_grow_with_confirmed_history(self):
        def steps(history):
            path = Path(self.tmp.name) / f"history-{history}.sqlite"
            store = MqttStore(
                path,
                bridge_id="pi-01",
                consumer_id="backend-01",
                mapping=MAPPING,
                clock_trusted=lambda: True,
            )
            self.addCleanup(store.db.close)
            with store.db:
                store.db.executemany(
                    "INSERT INTO mqtt_outbox(publication_id,logical_key,topic,payload,"
                    "consumer_id,state) VALUES (?,?,?,?,?,'completed')",
                    (
                        (f"done-{n}", f"done-{n}", "t", b"{}", "backend-01")
                        for n in range(history)
                    ),
                )
            store.accept(event(), 1800000000)
            count = [0]
            store.db.set_progress_handler(
                lambda: count.__setitem__(0, count[0] + 1) or 0, 100
            )
            for seq in range(2, 7):
                store.accept(event("HEARTBEAT", seq=seq), 1800000000)
            store.db.set_progress_handler(None, 0)
            return count[0]

        small, large = steps(20), steps(4000)
        self.assertLess(large, 2 * small + 20)

    def test_unrelated_client_suback_does_not_break_facade(self):
        _, client, worker = self.coordinator()
        worker.on_subscribe(999, [SimpleNamespace(is_failure=False, value=0)])
        self.ready(worker, client)
        self.assertIsNone(worker.fatal)

    def test_oversized_receipt_is_durably_rejected_before_native_ack(self):
        store, client, worker = self.coordinator()
        self.ready(worker, client)
        topic = "smartchair/v1/bridges/pi-01/receipts"
        worker.on_message(1, 25, 1, False, topic, Oversized("a" * 64), 1800000000)
        self.assertEqual(client.acks, [(25, 1)])
        self.assertEqual(
            store.db.execute("SELECT digest FROM mqtt_rejections").fetchone()[0],
            "a" * 64,
        )

    def test_failure_to_journal_bad_receipt_withholds_ack(self):
        store, client, worker = self.coordinator()
        self.ready(worker, client)
        store.db.executescript(
            "CREATE TRIGGER fail_reject BEFORE INSERT ON mqtt_rejections BEGIN SELECT RAISE(ABORT,'storage failure'); END;"
        )
        worker.on_message(
            1, 25, 1, False, "smartchair/v1/bridges/pi-01/receipts", b"bad", 1800000000
        )
        self.assertEqual(client.acks, [])
        self.assertTrue(worker.reconnect_required)


class FacadeTests(StoreFixture):
    def test_facade_attaches_to_caller_owned_client_and_does_not_manage_lifecycle(self):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
        client.connect = Mock(side_effect=AssertionError("facade must not connect"))
        client.disconnect = Mock(
            side_effect=AssertionError("facade must not disconnect")
        )
        client.loop_start = Mock(
            side_effect=AssertionError("facade must not start MQTT loop")
        )
        client.loop_stop = Mock(
            side_effect=AssertionError("facade must not stop MQTT loop")
        )
        client.subscribe = Mock(side_effect=[(0, 10), (0, 11)])
        mids = itertools.count(200)
        client.publish = Mock(
            side_effect=lambda *args, **kwargs: SimpleNamespace(rc=0, mid=next(mids))
        )
        client.ack = Mock(return_value=0)
        previous = Mock()
        client.on_connect = previous
        config = BridgeConfig(
            node_id=NODE,
            sensor_mac=MAC,
            bridge_id="pi-01",
            consumer_id="backend-01",
            database_path=self.path,
            allow_local_plaintext=True,
        )
        client._host = "127.0.0.1"
        bridge = ChairMqttBridge(client, config).attach()
        self.addCleanup(bridge.detach)
        self.assertTrue(client._manual_ack)
        self.assertTrue(client._will)
        self.assertEqual(
            bridge.connect_options()["properties"].SessionExpiryInterval, 86400
        )
        flags, success = (
            SimpleNamespace(session_present=False),
            SimpleNamespace(is_failure=False),
        )
        client.on_connect(client, None, flags, success, None)
        self.assertTrue(bridge.health().mqtt_connected)
        previous.assert_called_once()
        # A SUBACK for another application subscription must not break readiness.
        client.on_subscribe(
            client, None, 99, [SimpleNamespace(is_failure=False, value=0)], None
        )
        for mid in (10, 11):
            client.on_subscribe(
                client, None, mid, [SimpleNamespace(is_failure=False, value=1)], None
            )
        self.assertTrue(bridge.health().subscriptions_ready)
        request = json.loads(self.command())
        request["expires_at_ms"] = int(time.time() * 1000) + 60000
        message = SimpleNamespace(
            topic="smartchair/v1/nodes/chair-01/commands/config",
            payload=json.dumps(request).encode(),
            mid=77,
            qos=1,
            retain=False,
        )
        bridge._on_message(client, None, message)
        notice = bridge.receive(timeout=2)
        self.assertEqual(notice.disposition, "queued")
        self.assertEqual(notice.request.message.node_id, NODE)
        bridge.health()  # Synchronize with processing of the incoming acknowledgement.
        client.ack.assert_called_with(77, 1)
        self.assertEqual(
            bridge.publish(StateSync.model_validate(event()), source_mac=MAC).status,
            "accepted",
        )
        self.assertEqual(bridge.publish(event(), source_mac=MAC).status, "duplicate")
        self.assertFalse(bridge.detach())
        # The simulated disconnect never confirmed removal of the subscriptions.
        # Keep manual acknowledgements and the topic callbacks in place, fail-closed.
        self.assertTrue(client._manual_ack)
        self.assertTrue(client._will)
        self.assertIs(client.on_connect, previous)
        client.connect.assert_not_called()
        client.disconnect.assert_not_called()
        client.loop_start.assert_not_called()
        client.loop_stop.assert_not_called()

    def test_publish_returns_sensor_ack_instead_of_queueing_it_for_radio(self):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
        config = BridgeConfig(
            node_id=NODE,
            sensor_mac=MAC,
            bridge_id="pi-01",
            consumer_id="backend-01",
            database_path=self.path,
        )
        bridge = ChairMqttBridge(client, config).attach()
        self.addCleanup(bridge.detach)
        first = bridge.publish(event(), source_mac=MAC)
        self.assertEqual(first.status, "accepted")
        self.assertEqual(first.ack.type, "EVENT_ACK")
        self.assertEqual(
            (first.ack.ack_boot_id, first.ack.ack_seq), (event()["boot_id"], 1)
        )
        again = bridge.publish(event(), source_mac=MAC)
        self.assertEqual(again.status, "duplicate")
        self.assertEqual(again.ack.ack_seq, 1)  # A retransmission needs its ACK again.
        movement = bridge.publish(
            event(
                "MOVEMENT_BATCH",
                seq=2,
                batch_start_ms=0,
                duration_ms=40,
                end_reason="stopped",
                samples=[dict(offset_ms=0, ax=1, ay=2, az=3, gx=4, gy=5, gz=6)],
            ),
            source_mac=MAC,
        )
        self.assertEqual((movement.status, movement.ack), ("accepted", None))
        self.assertIsNone(bridge.gateway_next())  # Nothing stale waits for the radio.
        bridge.gateway_opened()
        ready = bridge.publish(
            event("READY", seq=3, reason="scheduled", listen_remaining_ms=10000),
            source_mac=MAC,
        )
        self.assertEqual(
            (ready.status, ready.reason, ready.ack),
            ("ignored", "awaiting_fresh_connection", None),
        )

    def test_detach_before_connect_restores_client_settings(self):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
        config = BridgeConfig(
            node_id=NODE,
            sensor_mac=MAC,
            bridge_id="pi-01",
            consumer_id="backend-01",
            database_path=self.path,
        )
        bridge = ChairMqttBridge(client, config).attach()
        self.assertFalse(bridge.detach())
        self.assertFalse(client._manual_ack)
        self.assertFalse(client._will)

    def test_remote_plaintext_is_rejected_without_managing_connection(self):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
        client._host = "example.org"
        client.subscribe = Mock(side_effect=AssertionError("must not subscribe"))
        config = BridgeConfig(
            node_id=NODE,
            sensor_mac=MAC,
            bridge_id="pi-01",
            consumer_id="backend-01",
            database_path=self.path,
        )
        bridge = ChairMqttBridge(client, config).attach()
        self.addCleanup(bridge.detach)
        client.on_connect(
            client,
            None,
            SimpleNamespace(session_present=False),
            SimpleNamespace(is_failure=False),
            None,
        )
        health = bridge.health()
        self.assertFalse(health.subscriptions_ready)
        self.assertIn("plaintext", health.fatal)
        client.subscribe.assert_not_called()


class BackendLedgerTests(StoreFixture):
    def test_backend_commits_fact_once_and_requeues_duplicate_receipts(self):
        source = self.store()
        source.accept(event(), 1800000000)
        publication = source.db.execute(
            "SELECT topic,payload FROM mqtt_outbox"
        ).fetchone()
        db = sqlite3.connect(Path(self.tmp.name) / "backend.sqlite")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE application_facts (value TEXT NOT NULL)")
        db.commit()

        def on_fact(connection, route, value):
            connection.execute(
                "INSERT INTO application_facts VALUES (?)", (value["message"]["type"],)
            )

        ledger = BackendLedger(
            db,
            node_id=NODE,
            bridge_id="pi-01",
            consumer_id="backend-01",
            on_fact=on_fact,
        )
        self.assertTrue(ledger.ingest(publication["topic"], publication["payload"]))
        self.assertFalse(ledger.ingest(publication["topic"], publication["payload"]))
        self.assertEqual(
            db.execute("SELECT count(*) FROM application_facts").fetchone()[0], 1
        )
        receipt = ledger.pending_receipts()[0]
        self.assertEqual(
            json.loads(receipt[2])["publication_id"],
            json.loads(publication["payload"])["publication_id"],
        )
        ledger.confirmed(receipt[0])
        self.assertEqual(ledger.pending_receipts(), [])
        self.assertFalse(ledger.ingest(publication["topic"], publication["payload"]))
        self.assertEqual(len(ledger.pending_receipts()), 1)

    def test_application_storage_failure_rolls_back_receipt_and_fact(self):
        source = self.store()
        source.accept(event(), 1800000000)
        publication = source.db.execute(
            "SELECT topic,payload FROM mqtt_outbox"
        ).fetchone()
        db = sqlite3.connect(Path(self.tmp.name) / "backend.sqlite")
        self.addCleanup(db.close)

        def fail(connection, route, value):
            raise sqlite3.OperationalError("application storage failed")

        ledger = BackendLedger(
            db, node_id=NODE, bridge_id="pi-01", consumer_id="backend-01", on_fact=fail
        )
        with self.assertRaises(sqlite3.Error):
            ledger.ingest(publication["topic"], publication["payload"])
        self.assertEqual(
            db.execute("SELECT count(*) FROM chair_mqtt_backend_facts").fetchone()[0], 0
        )
        self.assertEqual(ledger.pending_receipts(), [])

    def test_bad_delivery_journal_is_bounded_and_hashes_full_topic(self):
        db = sqlite3.connect(Path(self.tmp.name) / "backend.sqlite")
        self.addCleanup(db.close)
        ledger = BackendLedger(
            db, node_id=NODE, bridge_id="pi-01", consumer_id="backend-01"
        )
        prefix = "smartchair/v1/nodes/" + "x" * 300
        ledger.reject(prefix + "a", b"{}", "invalid")
        ledger.reject(prefix + "b", b"{}", "invalid")
        ledger.reject(prefix + "a", b"{}", "invalid")
        rows = db.execute(
            "SELECT digest,topic FROM chair_mqtt_backend_rejections"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(len(row[1].encode()) <= 256 for row in rows))
        self.assertNotEqual(rows[0][0], rows[1][0])


class SerialAdapterTests(StoreFixture):
    def test_serial_gateway_ack_follows_durable_outbox_commit(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
        config = BridgeConfig(
            node_id=NODE,
            sensor_mac=MAC,
            bridge_id="pi-01",
            consumer_id="backend-01",
            database_path=self.path,
        )
        bridge = ChairMqttBridge(client, config).attach()
        self.addCleanup(bridge.detach)
        bridge.start_gateway(os.ttyname(slave))
        pending = bytearray()

        def read_message(kind):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if b"\n" in pending:
                    line, _, rest = pending.partition(b"\n")
                    pending[:] = rest
                    value = json.loads(line)
                    if value["kind"] == kind:
                        return value
                elif select.select([master], [], [], 0.1)[0]:
                    pending.extend(os.read(master, 8192))
            self.fail(f"missing {kind}")

        hello = read_message("hello")
        os.write(master, json.dumps(hello).encode() + b"\n")
        data = json.dumps(envelope(event())).encode() + b"\n"
        os.write(master, data[:20])
        os.write(master, data[20:])
        ack = read_message("event_ack")
        self.assertEqual(ack["message"]["ack_seq"], 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM mqtt_outbox").fetchone()[0], 1
            )

    def test_firmware_hello_with_fractional_tx_power_completes_handshake(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
        config = BridgeConfig(
            node_id=NODE,
            sensor_mac=MAC,
            bridge_id="pi-01",
            consumer_id="backend-01",
            database_path=self.path,
        )
        bridge = ChairMqttBridge(client, config).attach()
        self.addCleanup(bridge.detach)
        bridge.start_gateway(os.ttyname(slave))
        pending = bytearray()
        deadline = time.monotonic() + 5
        while b"\n" not in pending and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                pending.extend(os.read(master, 8192))
        request = json.loads(pending.partition(b"\n")[0])
        # Shape of HelloMessage in shared/include/json_messages.h; 34 quarter-dBm.
        reply = (
            b'{"kind":"hello","request_id":%d,"role":"gateway",'
            b'"mac":"02:00:00:00:00:02","peer_mac":"02:00:00:00:00:01",'
            b'"key_id":"k1","channel":1,"power_save":0,"tx_power_dbm":8.5,'
            b'"ready":true,"max_payload":250,"lifecycle_busy":false}\n'
        ) % request["request_id"]
        os.write(master, reply)
        deadline = time.monotonic() + 5
        while not bridge.health().gateway_ready and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(bridge.health().gateway_ready)


class ControllerLedgerTests(StoreFixture):
    def test_retry_and_reports_deduplicate_across_status_and_updates(self):
        source = self.store()
        db = sqlite3.connect(Path(self.tmp.name) / "controller.sqlite")
        self.addCleanup(db.close)
        controller = ControllerLedger(db, node_id=NODE)
        request = ConfigRequest.model_validate(json.loads(self.command()))
        self.assertTrue(controller.enqueue(request, now_ms=1800000000000))
        self.assertEqual(len(controller.due(now_ms=1800000000000)), 1)
        self.assertTrue(
            controller.submitted(request.message.command_id, now_ms=1800000000000)
        )
        self.assertEqual(controller.due(now_ms=1800000014999), [])
        self.assertEqual(len(controller.due(now_ms=1800000015000)), 1)
        self.assertFalse(controller.enqueue(request, now_ms=1800000060001))

        topic = "smartchair/v1/nodes/chair-01/commands/config"
        source.admit_command(topic, self.command(), now=1800000000)
        command_id = request.message.command_id
        prefix = f"smartchair/v1/nodes/chair-01/commands/{command_id}"
        status = source.db.execute(
            "SELECT report FROM mqtt_commands WHERE id=?", (command_id,)
        ).fetchone()[0]
        update = source.db.execute(
            "SELECT payload FROM mqtt_outbox WHERE topic=?", (prefix + "/updates",)
        ).fetchone()[0]
        self.assertTrue(
            controller.observe(prefix + "/status", status, qos=1, retain=True)
        )
        self.assertFalse(
            controller.observe(prefix + "/updates", update, qos=1, retain=False)
        )
        self.assertEqual(controller.get(command_id)["outcome"], "queued")
        self.assertEqual(controller.due(now_ms=1800000030000), [])
        conflicting = json.loads(status)
        conflicting["updated_at_ms"] += 1
        with self.assertRaises(ContractError):
            controller.observe(
                prefix + "/status",
                json.dumps(conflicting).encode(),
                qos=1,
                retain=False,
            )

    def test_expiry_is_unknown_until_a_valid_late_report_arrives(self):
        db = sqlite3.connect(Path(self.tmp.name) / "controller.sqlite")
        self.addCleanup(db.close)
        controller = ControllerLedger(db, node_id=NODE)
        request = ConfigRequest.model_validate(json.loads(self.command()))
        controller.enqueue(request, now_ms=1800000000000)
        self.assertEqual(controller.due(now_ms=1800000060000), [])
        self.assertEqual(
            controller.get(request.message.command_id)["outcome"], "admission_unknown"
        )
        source = self.store()
        source.accept(event(), 1800000000)
        source.admit_command(
            "smartchair/v1/nodes/chair-01/commands/config",
            self.command(),
            now=1800000000,
        )
        source.pending(1800000061)
        source.accept(
            event(
                "COMMAND_RESULT",
                seq=2,
                command_id=request.message.command_id,
                status="applied",
            ),
            1800000062,
        )
        update = source.db.execute(
            "SELECT topic,payload FROM mqtt_outbox WHERE topic LIKE '%/updates' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        self.assertTrue(
            controller.observe(update["topic"], update["payload"], qos=1, retain=False)
        )
        current = controller.get(request.message.command_id)
        self.assertEqual(
            (current["admitted"], current["outcome"], current["report"]["late_result"]),
            (True, "expired", True),
        )


if __name__ == "__main__":
    unittest.main()


class PackagingTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def run_python(self, code, *args):
        return subprocess.run(
            [sys.executable, "-c", code, *args],
            cwd=self.ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_package_ignores_unrelated_top_level_transport_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("transport", "protocol_bridge"):
                (Path(directory) / f"{name}.py").write_text(
                    "raise ImportError('an unrelated distribution shadowed this')\n"
                )
            result = self.run_python(
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "import chair_mqtt.facade, chair_mqtt.serial_gateway, chair_mqtt.store",
                directory,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bench_console_needs_only_pyserial(self):
        result = self.run_python(
            "import runpy, sys\n"
            "for name in ('paho', 'pydantic', 'jsonschema'):\n"
            "    sys.modules[name] = None\n"
            "sys.argv = ['tools/transport.py', '--help']\n"
            "runpy.run_path('tools/transport.py', run_name='__main__')\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage", result.stdout)
