import copy
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import backend_receipt_example as backend  # noqa: E402
import contract  # noqa: E402

FIXTURES = json.loads((ROOT / "fixtures.json").read_text())
BY_NAME = {x["name"]: x for x in FIXTURES}


def payload(fixture):
    return (
        fixture["raw_payload"].encode()
        if "raw_payload" in fixture
        else backend.compact(fixture["payload"])
    )


class ContractTests(unittest.TestCase):
    def test_conformance_fixtures(self):
        self.assertEqual(len(FIXTURES), 69)
        for fixture in FIXTURES:
            with self.subTest(fixture=fixture["name"]):
                args = dict(qos=fixture["qos"], retain=fixture["retain"])
                if fixture["valid"]:
                    contract.validate(fixture["topic"], payload(fixture), **args)
                else:
                    with self.assertRaises(contract.ContractError):
                        contract.validate(fixture["topic"], payload(fixture), **args)

    def test_context_binding(self):
        f = BY_NAME["config-complete"]
        for context in (
            dict(expected_site="other"),
            dict(node_epochs={"chair-01": "bad"}),
            dict(node_epochs={}),
        ):
            with (
                self.subTest(context=context),
                self.assertRaises(contract.ContractError),
            ):
                contract.validate(
                    f["topic"], payload(f), qos=1, retain=False, **context
                )
        f = BY_NAME["backend-receipt"]
        with self.assertRaises(contract.ContractError):
            contract.validate(
                f["topic"], payload(f), qos=1, retain=False, expected_consumer="other"
            )
        f = BY_NAME["state-sync-event"]
        with self.assertRaises(contract.ContractError):
            contract.validate(
                f["topic"], payload(f), qos=1, retain=False, expected_bridge="other"
            )

    def test_status_unknown_counters_are_null_not_fabricated_values(self):
        fixture = copy.deepcopy(BY_NAME["bridge-online"])
        for name in (
            "gateway_connected",
            "gateway_ready",
            "storage_ok",
            "pending_publications",
            "overdue_receipts",
            "quarantined_publications",
            "mqtt_best_effort_dropped",
        ):
            fixture["payload"][name] = None
        fixture["payload"]["subscriptions_ready"] = False
        contract.validate(fixture["topic"], payload(fixture), qos=1, retain=True)

    def test_topic_and_payload_bounds(self):
        for topic in [
            "smartchair/v1/lab/nodes/+/events",
            "smartchair/v1/lab/nodes/chair-01/#",
            "smartchair/v1/lab\n/nodes/chair-01/events",
            "x" * 257,
        ]:
            with self.subTest(topic=topic), self.assertRaises(contract.ContractError):
                contract.route(topic)
        f = BY_NAME["config-complete"]
        for raw in [
            b" " * 16385,
            b"\xff",
            b"[]",
            b'{"x":Infinity}',
            b'{"x":1e400}',
            b'{"x":true,"x":false}',
        ]:
            with self.subTest(raw=raw[:30]), self.assertRaises(contract.ContractError):
                contract.validate(f["topic"], raw, qos=1, retain=False)


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "backend.sqlite"
        self.ledger = backend.Ledger(self.path, "lab", "backend-01", ["pi-01"])
        self.addCleanup(lambda: self.ledger.close())
        self.fixture = BY_NAME["state-sync-event"]
        self.topic, self.raw = self.fixture["topic"], payload(self.fixture)

    def count(self, table):
        return self.ledger.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class LedgerTests(DatabaseCase):
    def test_ingestion_commit_visible_before_receipt_return(self):
        topic, raw = self.ledger.ingest(self.topic, self.raw)
        with closing(sqlite3.connect(self.path)) as other:
            self.assertEqual(
                other.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 1
            )
            self.assertEqual(
                other.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 1
            )
        contract.validate(
            topic, raw, qos=1, retain=False, expected_consumer="backend-01"
        )

    def test_duplicate_requeues_ack_after_broker_ack_without_duplicate_fact(self):
        first = self.ledger.ingest(self.topic, self.raw)
        publication = self.fixture["payload"]["publication_id"]
        self.ledger.confirmed(publication)
        self.assertEqual(self.count("receipts"), 0)
        self.assertEqual(self.ledger.ingest(self.topic, self.raw), first)
        self.assertEqual(
            (self.count("facts"), self.count("deliveries"), self.count("receipts")),
            (1, 1, 1),
        )

    def test_replay_new_publication_preserves_one_semantic_fact(self):
        self.ledger.ingest(self.topic, self.raw)
        value = copy.deepcopy(self.fixture["payload"])
        value["publication_id"] = "6c19f0de-eeb6-4c3a-9d8b-d029000a4405"
        self.ledger.ingest(self.topic, backend.compact(value))
        self.assertEqual((self.count("facts"), self.count("deliveries")), (1, 2))

    def test_publication_and_semantic_conflicts_preserve_original(self):
        self.ledger.ingest(self.topic, self.raw)
        # Even a whitespace change is a new byte payload under an existing immutable ID.
        with self.assertRaises(contract.ContractError):
            self.ledger.ingest(
                self.topic, json.dumps(self.fixture["payload"], indent=2).encode()
            )
        value = copy.deepcopy(self.fixture["payload"])
        value["publication_id"] = "6c19f0de-eeb6-4c3a-9d8b-d029000a4405"
        value["message"]["occupied"] = False
        with self.assertRaises(contract.ContractError):
            self.ledger.ingest(self.topic, backend.compact(value))
        self.assertEqual((self.count("facts"), self.count("deliveries")), (1, 1))

    def test_report_semantic_identity_is_command_and_report_sequence(self):
        f = BY_NAME["report-applied"]
        self.ledger.ingest(f["topic"], payload(f))
        value = copy.deepcopy(f["payload"])
        value["publication_id"] = "6c19f0de-eeb6-4c3a-9d8b-d029000a4405"
        value["status"], value["message"]["status"] = "rejected", "busy"
        with self.assertRaises(contract.ContractError):
            self.ledger.ingest(f["topic"], backend.compact(value))
        value["report_seq"] += 1
        self.ledger.ingest(f["topic"], backend.compact(value))
        self.assertEqual(self.count("facts"), 2)

    def test_epoch_separates_fact_namespaces_without_losing_old_history(self):
        self.ledger.ingest(self.topic, self.raw)
        value = copy.deepcopy(self.fixture["payload"])
        value.update(
            publication_id="6c19f0de-eeb6-4c3a-9d8b-d029000a4405",
            node_epoch="3e049e82-fdea-4f4e-ae86-d31f3dd89dcb",
        )
        self.ledger.ingest(self.topic, backend.compact(value))
        self.assertEqual(self.count("facts"), 2)

    def test_receipt_insert_failure_rolls_back_all_ingestion(self):
        self.ledger.db.executescript("""CREATE TRIGGER fail_receipt BEFORE INSERT ON receipts
            BEGIN SELECT RAISE(ABORT,'disk failure'); END;""")
        with self.assertRaises(sqlite3.Error):
            self.ledger.ingest(self.topic, self.raw)
        self.assertEqual(
            (self.count("facts"), self.count("deliveries"), self.count("receipts")),
            (0, 0, 0),
        )

    def test_pending_receipt_survives_process_database_reopen(self):
        self.ledger.ingest(self.topic, self.raw)
        original = self.ledger.pending()
        self.ledger.close()
        self.ledger = backend.Ledger(self.path, "lab", "backend-01", ["pi-01"])
        self.assertEqual(self.ledger.pending(), original)

    def test_unknown_bridge_and_non_stream_topic_are_rejected(self):
        value = copy.deepcopy(self.fixture["payload"])
        value["bridge_id"] = "other"
        with self.assertRaises(contract.ContractError):
            self.ledger.ingest(self.topic, backend.compact(value))
        f = BY_NAME["retained-state"]
        with self.assertRaises(contract.ContractError):
            self.ledger.ingest(f["topic"], payload(f), retain=True)
        self.assertEqual(self.count("deliveries"), 0)

    def test_unrelated_database_and_other_consumer_are_not_repurposed(self):
        other = Path(self.directory.name) / "other.sqlite"
        with closing(sqlite3.connect(other)) as db, db:
            db.execute("CREATE TABLE precious (value TEXT)")
            db.execute("INSERT INTO precious VALUES ('retained')")
        with self.assertRaises(ValueError):
            backend.Ledger(other, "lab", "backend-01", ["pi-01"])
        with closing(sqlite3.connect(other)) as db, db:
            self.assertEqual(
                db.execute("SELECT * FROM precious").fetchall(), [("retained",)]
            )
        with self.assertRaises(ValueError):
            backend.Ledger(self.path, "lab", "backend-02", ["pi-01"])


class FakeClient:
    def __init__(self):
        self.acks, self.published, self.next_mid = [], [], 100

    def ack(self, mid, qos):
        self.acks.append((mid, qos))
        return 0

    def publish(self, topic, payload, **flags):
        self.next_mid += 1
        self.published.append((topic, payload, flags))
        return SimpleNamespace(rc=0, mid=self.next_mid)

    def subscribe(self, topic, **flags):
        self.next_mid += 1
        return 0, self.next_mid


class WorkerTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.client = FakeClient()
        mqtt = SimpleNamespace(
            MQTT_ERR_SUCCESS=0,
            SubscribeOptions=lambda **kw: kw,
            Properties=lambda _: SimpleNamespace(),
            PacketTypes=SimpleNamespace(PUBLISH=3),
        )
        self.worker = backend.Backend(self.ledger, mqtt, self.client)
        self.worker.generation, self.worker.connected, self.worker.ready = 1, True, True

    def receive(self):
        message = SimpleNamespace(
            mid=7, qos=1, topic=self.topic, payload=self.raw, retain=False
        )
        self.worker.on_message(self.client, None, message)

    def test_callback_only_enqueues_and_worker_commits_before_ack(self):
        self.receive()
        self.assertEqual((self.count("deliveries"), self.client.acks), (0, []))
        self.worker.step(100)
        self.assertEqual((self.count("deliveries"), self.client.acks), (1, [(7, 1)]))
        self.assertEqual(len(self.client.published), 1)
        mid = next(iter(self.worker.flights))
        self.worker.on_publish(
            self.client, None, mid, SimpleNamespace(is_failure=False), None
        )
        self.assertEqual(self.count("receipts"), 1)  # Callback does no SQLite work.
        self.worker.step(101)
        self.assertEqual(self.count("receipts"), 0)

    def test_failed_commit_never_native_acks(self):
        self.ledger.db.executescript("""CREATE TRIGGER fail_receipt BEFORE INSERT ON receipts
            BEGIN SELECT RAISE(ABORT,'disk failure'); END;""")
        self.receive()
        with self.assertRaises(sqlite3.Error):
            self.worker.step(100)
        self.assertEqual(self.client.acks, [])

    def test_oversized_message_is_recorded_before_ack_and_valid_work_continues(self):
        oversized = SimpleNamespace(
            mid=8, qos=1, topic=self.topic, payload=b"x" * 16385, retain=False
        )
        self.worker.on_message(self.client, None, oversized)
        self.assertFalse(self.worker.recovery)

        def ack_after_commit(mid, qos):
            with closing(sqlite3.connect(self.path)) as other:
                self.assertEqual(
                    other.execute("SELECT COUNT(*) FROM rejected").fetchone()[0], 1
                )
            self.client.acks.append((mid, qos))
            return 0

        with patch.object(self.client, "ack", side_effect=ack_after_commit):
            self.worker.step(100)
        self.assertEqual(self.client.acks, [(8, 1)])
        self.assertEqual(
            (self.count("rejected"), self.count("deliveries"), self.count("receipts")),
            (1, 0, 0),
        )
        self.assertFalse(self.worker.recovery)
        self.receive()
        self.worker.step(101)
        self.assertEqual(self.client.acks, [(8, 1), (7, 1)])
        self.assertEqual(self.count("deliveries"), 1)

    def test_oversized_rejection_storage_failure_never_native_acks(self):
        self.ledger.db.executescript("""CREATE TRIGGER fail_rejection BEFORE INSERT ON rejected
            BEGIN SELECT RAISE(ABORT,'disk failure'); END;""")
        message = SimpleNamespace(
            mid=8, qos=1, topic=self.topic, payload=b"x" * 16385, retain=False
        )
        self.worker.on_message(self.client, None, message)
        with self.assertRaises(sqlite3.Error):
            self.worker.step(100)
        self.assertEqual(self.client.acks, [])
        self.assertEqual(self.count("rejected"), 0)

    def test_oversized_rejection_is_bounded_and_deduplicates_full_identity(self):
        topic_prefix = "smartchair/v1/lab/nodes/" + "x" * 300
        payload_bytes = b"x" * 60000
        messages = [
            SimpleNamespace(
                mid=mid,
                qos=1,
                topic=topic_prefix + suffix,
                payload=payload_bytes,
                retain=False,
            )
            for mid, suffix in [(8, "/a"), (9, "/b")]
        ]
        for message in messages:
            self.worker.on_message(self.client, None, message)
        for item in self.worker.work:
            self.assertLessEqual(len(item[3].encode("utf-8")), 256)
            self.assertLess(sys.getsizeof(item[4]), 512)
        self.worker.step(100)
        rows = self.ledger.db.execute(
            "SELECT digest,topic,reason FROM rejected"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row[0] for row in rows},
            {
                hashlib.sha256(m.topic.encode() + b"\0" + payload_bytes).hexdigest()
                for m in messages
            },
        )
        self.assertTrue(all(len(row[1].encode("utf-8")) <= 256 for row in rows))
        self.assertTrue(all(row[2] == "payload exceeds maximum" for row in rows))
        self.worker.on_message(self.client, None, messages[0])
        self.worker.step(101)
        self.assertEqual(self.count("rejected"), 2)
        self.assertEqual(self.client.acks, [(8, 1), (9, 1), (8, 1)])

    def test_long_topics_with_small_payload_are_bounded_and_deduplicated(self):
        prefix = "smartchair/v1/lab/nodes/" + "x" * 231 + "€" + "y" * 40
        messages = [
            SimpleNamespace(
                mid=mid, qos=1, topic=prefix + suffix, payload=b"{}", retain=False
            )
            for mid, suffix in [(8, "/a/events"), (9, "/b/events")]
        ]
        for message in messages:
            self.worker.on_message(self.client, None, message)

        def ack_after_commit(mid, qos):
            with closing(sqlite3.connect(self.path)) as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM rejected").fetchone()[0], mid - 7
                )
            self.client.acks.append((mid, qos))
            return 0

        with patch.object(self.client, "ack", side_effect=ack_after_commit):
            self.worker.step(100)
        rows = self.ledger.db.execute("SELECT digest,topic FROM rejected").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row[0] for row in rows},
            {
                hashlib.sha256(m.topic.encode() + b"\0" + m.payload).hexdigest()
                for m in messages
            },
        )
        self.assertTrue(all(len(row[1].encode("utf-8")) <= 256 for row in rows))
        self.assertTrue(all(row[1] == prefix[:255] for row in rows))
        self.assertEqual((self.count("deliveries"), self.count("receipts")), (0, 0))
        self.assertEqual(self.client.published, [])
        self.worker.on_message(self.client, None, messages[0])
        self.worker.step(101)
        self.assertEqual(self.count("rejected"), 2)
        self.assertEqual(self.client.acks, [(8, 1), (9, 1), (8, 1)])
        self.receive()
        self.worker.step(102)
        self.assertEqual(self.count("deliveries"), 1)

    def test_long_topic_rejection_storage_failure_never_native_acks(self):
        self.ledger.db.executescript("""CREATE TRIGGER fail_rejection BEFORE INSERT ON rejected
            BEGIN SELECT RAISE(ABORT,'disk failure'); END;""")
        message = SimpleNamespace(
            mid=8, qos=1, topic=self.topic + "x" * 300, payload=b"{}", retain=False
        )
        self.worker.on_message(self.client, None, message)
        with self.assertRaises(sqlite3.Error):
            self.worker.step(100)
        self.assertEqual((self.client.acks, self.count("rejected")), ([], 0))

    def test_subscriptions_require_qos_one_grants(self):
        self.worker.ready = False
        self.worker.subscriptions = {101, 102}
        grant = SimpleNamespace(is_failure=False, value=1)
        self.worker.on_subscribe(self.client, None, 101, [grant], None)
        self.assertFalse(self.worker.ready)
        self.worker.on_subscribe(self.client, None, 102, [grant], None)
        self.assertTrue(self.worker.ready)
        self.assertIsNone(self.worker.fatal)

    def test_downgraded_subscription_fails_closed(self):
        self.worker.ready = False
        self.worker.subscriptions = {101, 102}
        self.worker.on_subscribe(
            self.client, None, 101, [SimpleNamespace(is_failure=False, value=0)], None
        )
        self.assertFalse(self.worker.ready)
        self.assertTrue(self.worker.fatal)
        self.worker.on_subscribe(
            self.client, None, 102, [SimpleNamespace(is_failure=False, value=1)], None
        )
        self.assertFalse(self.worker.ready)
        self.worker.step(100)
        self.assertEqual(self.client.published, [])

    def test_native_ack_timeout_is_independent_of_logical_receipt_rows(self):
        self.receive()
        self.worker.step(100)
        self.ledger.confirmed(self.fixture["payload"]["publication_id"])
        self.worker.step(129.99)
        self.assertFalse(self.worker.recovery)
        self.worker.step(130)
        self.assertTrue(self.worker.recovery)
        self.assertEqual(len(self.client.published), 1)

    def test_negative_ack_preserves_durable_receipt_and_stops_retry(self):
        self.receive()
        self.worker.step(100)
        mid = next(iter(self.worker.flights))
        self.worker.on_publish(
            self.client, None, mid, SimpleNamespace(is_failure=True), None
        )
        self.worker.step(101)
        self.assertTrue(self.worker.fatal)
        self.assertEqual(self.count("receipts"), 1)
        self.assertEqual(len(self.client.published), 1)

    def test_stale_incoming_token_does_not_ack_current_connection(self):
        self.receive()
        self.worker.generation = 2
        self.worker.step(100)
        self.assertEqual((self.count("deliveries"), self.client.acks), (0, []))

    def test_fresh_session_mismatch_and_live_session_resume(self):
        self.worker.generation = 0
        self.worker.on_connect(
            self.client,
            None,
            SimpleNamespace(session_present=True),
            SimpleNamespace(is_failure=False),
            None,
        )
        self.assertTrue(self.worker.fatal)
        self.worker.fatal = None
        self.worker.on_connect(
            self.client,
            None,
            SimpleNamespace(session_present=False),
            SimpleNamespace(is_failure=False),
            None,
        )
        self.assertEqual(self.worker.generation, 1)
        self.worker.flights[42] = ("publication", 130)
        self.receive()
        self.worker.on_disconnect(self.client, None, None, None, None)
        self.assertFalse(self.worker.work)
        with patch.object(backend.time, "monotonic", return_value=200):
            self.worker.on_connect(
                self.client,
                None,
                SimpleNamespace(session_present=True),
                SimpleNamespace(is_failure=False),
                None,
            )
        self.assertEqual(self.worker.generation, 2)
        self.assertEqual(self.worker.flights[42][1], 230)
        self.assertFalse(self.worker.fatal)

    def test_subscription_ack_timeout_requires_recovery(self):
        self.worker.ready = False
        self.worker.subscription_deadline = 130
        self.worker.step(130)
        self.assertTrue(self.worker.recovery)


if __name__ == "__main__":
    unittest.main()
