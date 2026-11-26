import asyncio
import json
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import paho.mqtt.client as mqtt

from chair_mqtt import (
    BrokerConfig,
    ChairClient,
    ChairConfiguration,
    ClientConfig,
    SQLiteStorage,
    connection,
)
from chair_mqtt.store import MqttStore

MOSQUITTO = shutil.which("mosquitto")
CONFIG = ClientConfig(node_id="chair-01", bridge_id="pi-01", consumer_id="backend-01")


def loopback(port):
    return BrokerConfig(
        host="127.0.0.1", port=port, client_id="backend-test", plaintext=True
    )


def free_port():
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        return reserved.getsockname()[1]


def start_broker(test, directory):
    """Run a private loopback Mosquitto for the test and return its port."""
    port = free_port()
    conf = Path(directory) / "mosquitto.conf"
    conf.write_text(
        f"listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n"
    )
    broker = subprocess.Popen(
        [MOSQUITTO, "-c", str(conf)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    test.addCleanup(broker.wait, timeout=5)
    test.addCleanup(broker.terminate)
    deadline = time.monotonic() + 5
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return port
        except OSError:
            if time.monotonic() >= deadline:
                test.fail("private broker did not start")
            time.sleep(0.05)


class AsyncClientLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_does_not_wait_for_an_unreachable_broker(self):
        with tempfile.TemporaryDirectory() as directory:
            chair = ChairClient(
                CONFIG,
                SQLiteStorage(Path(directory) / "backend.db"),
                loopback(free_port()),
            )
            started = time.monotonic()
            async with chair:
                await asyncio.sleep(0.3)
                self.assertFalse(chair.connected)
            self.assertLess(time.monotonic() - started, 3)

    @unittest.skipUnless(MOSQUITTO, "private Mosquitto broker unavailable")
    async def test_failed_event_handler_is_redelivered_after_the_client_reconnects(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            port = start_broker(self, directory)
            chair = ChairClient(
                CONFIG, SQLiteStorage(Path(directory) / "backend.db"), loopback(port)
            )
            attempts = []

            @chair.on_event
            async def flaky(event, transaction):
                attempts.append(event.message.seq)
                if len(attempts) == 1:
                    raise RuntimeError("application not ready yet")

            async def until(predicate):
                for _ in range(200):
                    if predicate():
                        return
                    await asyncio.sleep(0.05)
                self.fail(f"timed out: {chair.error}")

            topic, payload = sensor_publication(directory)
            publisher = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5
            )
            publisher.connect("127.0.0.1", port)
            publisher.loop_start()
            self.addCleanup(publisher.loop_stop)
            self.addCleanup(publisher.disconnect)
            async with chair:
                await until(lambda: chair.ready)
                with self.assertLogs("chair_mqtt.client", level="ERROR"):
                    publisher.publish(topic, payload, qos=1)
                    # Without application help, the client drops the connection so
                    # the broker redelivers the unacknowledged event.
                    await until(lambda: attempts == [1, 1] and chair.ready)
                self.assertFalse(chair.reconnect_required)


@unittest.skipUnless(MOSQUITTO, "private Mosquitto broker unavailable")
class AsyncClientBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_receipt_and_async_command_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            port = start_broker(self, directory)
            observer = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5
            )
            messages = []
            observer.on_message = lambda c, u, m: messages.append(
                (m.topic, bytes(m.payload))
            )
            observer.connect("127.0.0.1", port)
            observer.subscribe("smartchair/v1/#", qos=1)
            observer.loop_start()
            self.addCleanup(observer.loop_stop)
            self.addCleanup(observer.disconnect)

            chair = ChairClient(
                CONFIG, SQLiteStorage(Path(directory) / "backend.db"), loopback(port)
            )
            received = []

            @chair.on_event
            async def event_handler(event, tx):
                await tx.execute(
                    "CREATE TABLE IF NOT EXISTS application_events (seq INTEGER)"
                )
                await tx.execute(
                    "INSERT INTO application_events VALUES (?)", (event.message.seq,)
                )
                received.append(event.message.seq)

            async with chair:

                async def until(predicate):
                    for _ in range(100):
                        if predicate():
                            return
                        await asyncio.sleep(0.05)
                    self.fail(f"timed out: {chair.error}")

                await until(lambda: chair.ready)
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
                    time.time(),
                )
                event = source.db.execute(
                    "SELECT topic,payload FROM mqtt_outbox"
                ).fetchone()
                observer.publish(event[0], event[1], qos=1)
                await until(
                    lambda: (
                        received == [1]
                        and any(t.endswith("/receipts") for t, p in messages)
                    )
                )
                # Drop the network; the client reconnects and resumes its session.
                connection.abort(chair._mqtt)
                await until(lambda: not chair.connected)
                heartbeat = dict(
                    version=2,
                    type="HEARTBEAT",
                    node_id="chair-01",
                    boot_id="0000000000000001",
                    seq=2,
                    uptime_ms=200,
                    occupied=True,
                    battery_mv=None,
                    config_revision=0,
                    health_flags=0,
                )
                source.accept(heartbeat, time.time())
                event = source.db.execute(
                    "SELECT topic,payload FROM mqtt_outbox ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                observer.publish(event[0], event[1], qos=1).wait_for_publish(timeout=3)
                await until(lambda: chair.ready and received == [1, 2])
                command = await chair.set_config(
                    ChairConfiguration(
                        revision=1,
                        heartbeat_s=900,
                        movement_batch_s=3,
                        movement_enabled=True,
                        radio_windows_enabled=True,
                        radio_interval_s=30,
                        radio_open_s=5,
                        radio_motion_tail_s=10,
                    ),
                    expires_in_seconds=60,
                )
                await until(
                    lambda: any(t.endswith("/commands/config") for t, p in messages)
                )
                command_topic, command_payload = next(
                    (t, p) for t, p in messages if t.endswith("/commands/config")
                )
                self.assertEqual(
                    json.loads(command_payload)["message"]["command_id"],
                    command.command_id,
                )
                source.admit_command(command_topic, command_payload, now=time.time())
                status = source.db.execute(
                    "SELECT topic,desired FROM mqtt_projection WHERE topic LIKE '%/commands/%/status'"
                ).fetchone()
                observer.publish(status[0], status[1], qos=1, retain=True)
                await until(lambda: any(t == status[0] for t, p in messages))
                for _ in range(100):
                    state = await chair.get_command(command.command_id)
                    if state.status == "queued":
                        break
                    await asyncio.sleep(0.05)
                self.assertEqual(state.status, "queued")
                with self.assertRaises(asyncio.TimeoutError):
                    await command.wait(timeout=0.05)
                source.accept(
                    dict(
                        version=2,
                        type="COMMAND_RESULT",
                        node_id="chair-01",
                        boot_id="0000000000000001",
                        seq=3,
                        uptime_ms=300,
                        occupied=True,
                        battery_mv=None,
                        config_revision=1,
                        health_flags=0,
                        command_id=command.command_id,
                        status="applied",
                    ),
                    time.time(),
                )
                report = source.db.execute(
                    "SELECT topic,payload FROM mqtt_outbox WHERE topic LIKE '%/commands/%/updates' ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                observer.publish(report[0], report[1], qos=1)
                result = await command.wait(timeout=5)
                self.assertEqual(result.status, "applied")
                source.db.close()


def sensor_publication(directory):
    """Return (topic, payload) of a reliable event as the Pi bridge publishes it."""
    source = MqttStore(
        Path(directory) / "source.db",
        bridge_id="pi-01",
        consumer_id="backend-01",
        mapping={"chair-01": "02:00:00:00:00:01"},
        clock_trusted=lambda: True,
    )
    try:
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
        return row[0], row[1]
    finally:
        source.db.close()


class AsyncClientUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_receipt_queued_by_paho_while_disconnected_is_tracked(self):
        with tempfile.TemporaryDirectory() as directory:
            topic, payload = sensor_publication(directory)
            storage = SQLiteStorage(Path(directory) / "backend.db")
            await storage.initialize("chair-01", "pi-01", "backend-01")
            self.addAsyncCleanup(storage.close)
            await storage.ingest(
                topic, payload, qos=1, retain=False, on_fact=lambda *a: asyncio.sleep(0)
            )
            client = Mock()
            client.publish.return_value = SimpleNamespace(
                rc=mqtt.MQTT_ERR_NO_CONN, mid=7
            )
            chair = ChairClient(CONFIG, storage, loopback(1))
            chair._mqtt = client
            chair._connected = chair._ready = True
            await chair._tick()
            await chair._tick()
            self.assertEqual(client.publish.call_count, 1)
            self.assertEqual(
                chair._flights[7][:2],
                ("receipt", (await storage.pending_receipts())[0][0]),
            )

    async def test_failing_notification_handler_is_logged_and_delivery_acked(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = SQLiteStorage(Path(directory) / "backend.db")
            await storage.initialize("chair-01", "pi-01", "backend-01")
            self.addAsyncCleanup(storage.close)
            client = Mock()
            client.ack.return_value = mqtt.MQTT_ERR_SUCCESS
            chair = ChairClient(CONFIG, storage, loopback(1))
            chair._mqtt = client
            chair._connected = True
            chair._generation = chair._network_generation = 1
            seen = []

            @chair.on_bridge_status
            async def broken(status):
                raise KeyError("application bug")

            @chair.on_bridge_status
            async def healthy(status):
                seen.append(status.status)

            status = (
                b'{"schema_version":1,"type":"BRIDGE_STATUS","bridge_id":"pi-01",'
                b'"instance_id":"5fd84ecd-e28f-48fb-9b9b-f7041f040461",'
                b'"status":"online","reason":null,"observed_at_ms":1800000000123,'
                b'"gateway_connected":true,"gateway_ready":true,"storage_ok":true,'
                b'"subscriptions_ready":true,"pending_publications":0,'
                b'"overdue_receipts":0,"quarantined_publications":0,'
                b'"mqtt_best_effort_dropped":0}'
            )
            with self.assertLogs("chair_mqtt.client", level="ERROR"):
                await chair._incoming(
                    1, 42, 1, True, "smartchair/v1/bridges/pi-01/status", status
                )
            self.assertFalse(chair.reconnect_required)
            self.assertEqual(seen, ["online"])
            client.ack.assert_called_once_with(42, 1)

    async def command_client(self, directory):
        storage = SQLiteStorage(Path(directory) / "backend.db")
        calls = []
        original = storage.get_command

        async def counted(command_id):
            calls.append(command_id)
            return await original(command_id)

        storage.get_command = counted
        chair = ChairClient(CONFIG, storage, loopback(1))
        chair._mqtt = Mock()  # Offline: tests feed deliveries to _incoming directly.
        chair._mqtt.ack.return_value = mqtt.MQTT_ERR_SUCCESS
        await chair.start()
        chair._connected = True
        chair._generation = chair._network_generation = 1
        return chair, storage, calls

    async def test_command_wait_sleeps_until_its_report_advances(self):
        with tempfile.TemporaryDirectory() as directory:
            chair, storage, calls = await self.command_client(directory)
            self.addAsyncCleanup(chair.stop)
            command = await chair.set_config(
                ChairConfiguration(
                    revision=1,
                    heartbeat_s=900,
                    movement_batch_s=3,
                    movement_enabled=True,
                    radio_windows_enabled=True,
                    radio_interval_s=30,
                    radio_open_s=5,
                    radio_motion_tail_s=10,
                ),
                expires_in_seconds=60,
            )
            waiter = asyncio.create_task(command.wait(timeout=10))
            await asyncio.sleep(1.0)
            self.assertFalse(waiter.done())
            self.assertLessEqual(calls.count(command.command_id), 2)

            # The bridge admits the command and the sensor applies it.
            now = time.time()
            ((_, topic, payload, _),) = await storage.due_commands(int(now * 1000))
            source = MqttStore(
                Path(directory) / "source.db",
                bridge_id="pi-01",
                consumer_id="backend-01",
                mapping={"chair-01": "02:00:00:00:00:01"},
                clock_trusted=lambda: True,
            )
            self.addCleanup(source.db.close)
            source.admit_command(topic, payload, now=now)
            source.accept(
                dict(
                    version=2,
                    type="COMMAND_RESULT",
                    node_id="chair-01",
                    boot_id="0000000000000001",
                    seq=1,
                    uptime_ms=300,
                    occupied=True,
                    battery_mv=None,
                    config_revision=1,
                    health_flags=0,
                    command_id=command.command_id,
                    status="applied",
                ),
                now,
            )
            update = source.db.execute(
                "SELECT topic,payload FROM mqtt_outbox WHERE topic LIKE '%/updates' "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            await chair._incoming(1, 9, 1, False, update[0], update[1])
            result = await asyncio.wait_for(waiter, 1)
            self.assertEqual(result.status, "applied")

    async def test_command_wait_reports_unknown_admission_at_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            chair, _, _ = await self.command_client(directory)
            self.addAsyncCleanup(chair.stop)
            command = await chair.set_config(
                ChairConfiguration(
                    revision=1,
                    heartbeat_s=900,
                    movement_batch_s=3,
                    movement_enabled=True,
                    radio_windows_enabled=True,
                    radio_interval_s=30,
                    radio_open_s=5,
                    radio_motion_tail_s=10,
                ),
                expires_in_seconds=1,
            )
            result = await command.wait(timeout=3)
            self.assertEqual(result.status, "admission_unknown")

    async def test_stop_releases_an_unbounded_command_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            chair, _, _ = await self.command_client(directory)
            command = await chair.set_config(
                ChairConfiguration(
                    revision=1,
                    heartbeat_s=900,
                    movement_batch_s=3,
                    movement_enabled=True,
                    radio_windows_enabled=True,
                    radio_interval_s=30,
                    radio_open_s=5,
                    radio_motion_tail_s=10,
                ),
                expires_in_seconds=60,
            )
            waiter = asyncio.create_task(command.wait())
            await asyncio.sleep(0.1)
            await chair.stop()
            with self.assertRaises(RuntimeError):
                await asyncio.wait_for(waiter, 2)
