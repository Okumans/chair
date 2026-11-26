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

from chair_mqtt import ChairClient, ChairConfiguration, ClientConfig, SQLiteStorage
from chair_mqtt.store import MqttStore

MOSQUITTO = shutil.which("mosquitto")


@unittest.skipUnless(MOSQUITTO, "private Mosquitto broker unavailable")
class AsyncClientBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_receipt_and_async_command_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            with socket.socket() as reserved:
                reserved.bind(("127.0.0.1", 0))
                port = reserved.getsockname()[1]
            conf = Path(directory) / "mosquitto.conf"
            conf.write_text(
                f"listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n"
            )
            broker = subprocess.Popen(
                [MOSQUITTO, "-c", str(conf)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.addCleanup(broker.wait, timeout=5)
            self.addCleanup(broker.terminate)
            await asyncio.sleep(0.2)
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

            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id="backend-test",
                protocol=mqtt.MQTTv5,
            )
            chair = ChairClient(
                client,
                ClientConfig(
                    node_id="chair-01",
                    bridge_id="pi-01",
                    consumer_id="backend-01",
                    allow_local_plaintext=True,
                ),
                SQLiteStorage(Path(directory) / "backend.db"),
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

            await chair.attach()
            client.connect("127.0.0.1", port, **chair.connect_options())
            client.loop_start()
            try:

                async def until(predicate):
                    for _ in range(100):
                        if predicate():
                            return
                        await asyncio.sleep(0.05)
                    self.fail(f"timed out: {chair.error}")

                await until(lambda: chair._ready)
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
                client.disconnect()
                await until(lambda: not chair._connected)
                client.loop_stop()
                # Paho 2.x does not close the loop socketpair in loop_stop.
                client._reset_sockets(sockpair_only=True)
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
                client.reconnect()
                client.loop_start()
                await until(lambda: chair._ready and received == [1, 2])
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
                self.assertTrue(await chair.detach())
            finally:
                client.disconnect()
                client.loop_stop()


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
            chair = ChairClient(
                client,
                ClientConfig(
                    node_id="chair-01", bridge_id="pi-01", consumer_id="backend-01"
                ),
                storage,
            )
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
            chair = ChairClient(
                client,
                ClientConfig(
                    node_id="chair-01", bridge_id="pi-01", consumer_id="backend-01"
                ),
                storage,
            )
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
