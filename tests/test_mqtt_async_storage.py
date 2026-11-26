import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

from chair_mqtt import ChairClient, ClientConfig, ConfigRequest
from chair_mqtt.async_storage import SQLiteStorage
from chair_mqtt.store import MqttStore


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_connector_contract_for_sqlite_and_custom_wrapper(self):
        class CustomConnector:
            def __init__(self, path):
                self.delegate = SQLiteStorage(path)

            def __getattr__(self, name):
                return getattr(self.delegate, name)

        with tempfile.TemporaryDirectory() as directory:
            for connector_type in (SQLiteStorage, CustomConnector):
                with self.subTest(connector=connector_type.__name__):
                    path = Path(directory) / (connector_type.__name__ + ".db")
                    storage = connector_type(path)
                    await storage.initialize("chair-01", "pi-01", "backend-01")
                    request = ConfigRequest.model_validate(
                        {
                            "schema_version": 1,
                            "expires_at_ms": 1800000060000,
                            "message": {
                                "version": 2,
                                "type": "CONFIG_SET",
                                "node_id": "chair-01",
                                "command_id": "37c7984c-33eb-4dcc-93be-e2b11bb2436d",
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
                    )
                    self.assertTrue(
                        await storage.enqueue_command(request, 1800000000000)
                    )
                    due = await storage.due_commands(1800000000000)
                    self.assertEqual(due[0][0], request.message.command_id)
                    self.assertTrue(
                        await storage.command_submitted(
                            request.message.command_id, 1800000000000
                        )
                    )
                    self.assertEqual(await storage.due_commands(1800000014999), [])
                    self.assertEqual(
                        (await storage.get_command(request.message.command_id))[
                            "outcome"
                        ],
                        None,
                    )
                    self.assertEqual(await storage.due_commands(1800000060000), [])
                    self.assertEqual(
                        (await storage.get_command(request.message.command_id))[
                            "outcome"
                        ],
                        "admission_unknown",
                    )
                    await storage.close()

    async def test_client_accepts_a_custom_storage_connector(self):
        with tempfile.TemporaryDirectory() as directory:
            source = MqttStore(
                Path(directory) / "source.db",
                bridge_id="pi-01",
                consumer_id="backend-01",
                mapping={"chair-01": "02:00:00:00:00:01"},
                clock_trusted=lambda: True,
            )
            source.accept(
                dict(
                    version=2,
                    type="STATE_SYNC",
                    node_id="chair-01",
                    boot_id="0000000000000001",
                    boot_generation="0000000000000001",
                    seq=1,
                    uptime_ms=100,
                    occupied=True,
                    battery_mv=None,
                    config_revision=0,
                    health_flags=0,
                ),
                1800000000,
            )
            topic, payload = source.db.execute(
                "SELECT topic,payload FROM mqtt_outbox"
            ).fetchone()
            source.db.close()

            class CustomStorage:
                def __init__(self):
                    self.committed = []

                async def ingest(self, topic, payload, *, qos, retain, on_fact):
                    from chair_mqtt.contract import validate

                    target, value = validate(topic, payload, qos=qos, retain=retain)
                    transaction = object()
                    await on_fact(target, value, transaction)
                    self.committed.append(value["publication_id"])
                    return True

            storage = CustomStorage()
            mqtt_client = Mock()
            mqtt_client.ack.return_value = 0
            chair = ChairClient(
                mqtt_client,
                ClientConfig(
                    node_id="chair-01", bridge_id="pi-01", consumer_id="backend-01"
                ),
                storage,
            )
            chair._connected = True
            chair._generation = chair._network_generation = 1
            seen = []

            @chair.on_event
            async def accept(event, transaction):
                seen.append((event.message.seq, transaction))

            await chair._incoming(1, 42, 1, False, topic, payload)
            self.assertEqual(seen[0][0], 1)
            self.assertEqual(len(storage.committed), 1)
            mqtt_client.ack.assert_called_once_with(42, 1)

    async def test_failed_callback_withholds_mqtt_acknowledgement(self):
        with tempfile.TemporaryDirectory() as directory:
            source = MqttStore(
                Path(directory) / "source.db",
                bridge_id="pi-01",
                consumer_id="backend-01",
                mapping={"chair-01": "02:00:00:00:00:01"},
                clock_trusted=lambda: True,
            )
            source.accept(
                dict(
                    version=2,
                    type="STATE_SYNC",
                    node_id="chair-01",
                    boot_id="0000000000000001",
                    boot_generation="0000000000000001",
                    seq=1,
                    uptime_ms=100,
                    occupied=True,
                    battery_mv=None,
                    config_revision=0,
                    health_flags=0,
                ),
                1800000000,
            )
            topic, payload = source.db.execute(
                "SELECT topic,payload FROM mqtt_outbox"
            ).fetchone()
            source.db.close()
            storage = SQLiteStorage(Path(directory) / "backend.db")
            await storage.initialize("chair-01", "pi-01", "backend-01")
            client = Mock()
            chair = ChairClient(
                client,
                ClientConfig(
                    node_id="chair-01", bridge_id="pi-01", consumer_id="backend-01"
                ),
                storage,
            )
            chair._connected = True
            chair._generation = chair._network_generation = 1

            @chair.on_event
            async def fail(event, transaction):
                raise RuntimeError("application failed")

            with self.assertLogs("chair_mqtt.client", level="ERROR"):
                await chair._incoming(1, 42, 1, False, topic, payload)
            self.assertTrue(chair.reconnect_required)
            client.ack.assert_not_called()
            self.assertEqual(await storage.pending_receipts(), [])
            await storage.close()

    async def test_fact_callback_and_receipt_commit_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            source = MqttStore(
                Path(directory) / "source.db",
                bridge_id="pi-01",
                consumer_id="backend-01",
                mapping={"chair-01": "02:00:00:00:00:01"},
                clock_trusted=lambda: True,
            )
            source.accept(
                dict(
                    version=2,
                    type="STATE_SYNC",
                    node_id="chair-01",
                    boot_id="0000000000000001",
                    boot_generation="0000000000000001",
                    seq=1,
                    uptime_ms=100,
                    occupied=True,
                    battery_mv=None,
                    config_revision=0,
                    health_flags=0,
                ),
                1800000000,
            )
            row = source.db.execute("SELECT topic,payload FROM mqtt_outbox").fetchone()
            topic, payload = row[0], row[1]
            source.db.close()
            path = Path(directory) / "backend.db"
            storage = SQLiteStorage(path)
            await storage.initialize("chair-01", "pi-01", "backend-01")
            calls = []

            async def fail(route, value, tx):
                await tx.execute("CREATE TABLE facts (payload TEXT)")
                await tx.execute("INSERT INTO facts VALUES (?)", (json.dumps(value),))
                raise RuntimeError("rollback")

            with self.assertRaisesRegex(RuntimeError, "rollback"):
                await storage.ingest(topic, payload, qos=1, retain=False, on_fact=fail)
            self.assertEqual(await storage.pending_receipts(), [])
            with closing(sqlite3.connect(path)) as db:
                self.assertIsNone(
                    db.execute(
                        "SELECT name FROM sqlite_master WHERE name='facts'"
                    ).fetchone()
                )

            async def accept(route, value, tx):
                calls.append(value["message"]["seq"])
                await tx.execute("CREATE TABLE facts (seq INTEGER)")
                await tx.execute(
                    "INSERT INTO facts VALUES (?)", (value["message"]["seq"],)
                )
                await asyncio.sleep(0)

            self.assertTrue(
                await storage.ingest(
                    topic, payload, qos=1, retain=False, on_fact=accept
                )
            )
            self.assertFalse(
                await storage.ingest(
                    topic, payload, qos=1, retain=False, on_fact=accept
                )
            )
            self.assertEqual(calls, [1])
            self.assertEqual(len(await storage.pending_receipts()), 1)
            await storage.close()

    async def test_storage_call_inside_fact_callback_raises_instead_of_hanging(self):
        with tempfile.TemporaryDirectory() as directory:
            source = MqttStore(
                Path(directory) / "source.db",
                bridge_id="pi-01",
                consumer_id="backend-01",
                mapping={"chair-01": "02:00:00:00:00:01"},
                clock_trusted=lambda: True,
            )
            source.accept(
                dict(
                    version=2,
                    type="STATE_SYNC",
                    node_id="chair-01",
                    boot_id="0000000000000001",
                    boot_generation="0000000000000001",
                    seq=1,
                    uptime_ms=100,
                    occupied=True,
                    battery_mv=None,
                    config_revision=0,
                    health_flags=0,
                ),
                1800000000,
            )
            topic, payload = source.db.execute(
                "SELECT topic,payload FROM mqtt_outbox"
            ).fetchone()
            source.db.close()
            storage = SQLiteStorage(Path(directory) / "backend.db")
            await storage.initialize("chair-01", "pi-01", "backend-01")
            self.addAsyncCleanup(storage.close)

            async def reenter(route, value, tx):
                # The timeout only keeps a regression from hanging the suite.
                await asyncio.wait_for(storage.pending_receipts(), 1)

            with self.assertRaisesRegex(RuntimeError, "transaction callback"):
                await storage.ingest(
                    topic, payload, qos=1, retain=False, on_fact=reenter
                )
            self.assertEqual(await storage.pending_receipts(), [])

            later = []

            async def schedule(route, value, tx):
                later.append(asyncio.create_task(storage.pending_receipts()))

            self.assertTrue(
                await storage.ingest(
                    topic, payload, qos=1, retain=False, on_fact=schedule
                )
            )
            self.assertEqual(len(await later[0]), 1)
