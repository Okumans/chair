"""End-to-end facade test against a private loopback MQTT broker."""

import io
import json
import queue
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path

import paho.mqtt.client as mqtt

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from chair_mqtt import BridgeConfig, ChairMqttBridge  # noqa: E402

BROKER = shutil.which("mosquitto")
NODE = "chair-01"
MAC = "02:00:00:00:00:01"
COMMAND = "37c7984c-33eb-4dcc-93be-e2b11bb2436d"


def event(kind="STATE_SYNC", seq=1, **fields):
    value = dict(
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
        value["boot_generation"] = "0000000000000001"
    value.update(fields)
    return value


@unittest.skipUnless(BROKER, "private Mosquitto broker unavailable")
class PrivateBrokerTests(unittest.TestCase):
    def test_caller_owned_client_sensor_command_and_backend_receipts(self):
        with tempfile.TemporaryDirectory(prefix="chair-mqtt-broker-") as temporary:
            directory = Path(temporary)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            config_file = directory / "mosquitto.conf"
            config_file.write_text(
                f"listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n"
            )
            with (directory / "broker.log").open("w+") as log:
                broker = subprocess.Popen(
                    [BROKER, "-c", str(config_file)], stdout=log, stderr=log
                )
                observer = None
                app_client = None
                bridge = None
                try:
                    end = time.monotonic() + 5
                    while True:
                        if broker.poll() is not None:
                            self.fail("private broker exited")
                        try:
                            with socket.create_connection(
                                ("127.0.0.1", port), timeout=0.1
                            ):
                                break
                        except OSError:
                            if time.monotonic() >= end:
                                self.fail("private broker did not start")
                            time.sleep(0.05)

                    messages = queue.Queue()
                    subscribed = threading.Event()
                    observer = mqtt.Client(
                        mqtt.CallbackAPIVersion.VERSION2,
                        client_id="chair-test-observer",
                        protocol=mqtt.MQTTv5,
                    )
                    observer.on_subscribe = lambda *args: subscribed.set()
                    observer.on_connect = (
                        lambda client, userdata, flags, reason, properties: (
                            client.subscribe("smartchair/v1/#", qos=1)
                        )
                    )

                    def observe(client, userdata, message):
                        if not message.payload:
                            messages.put((message.topic, None))
                            return
                        data = json.loads(message.payload)
                        messages.put((message.topic, data))
                        if message.topic.endswith("/events") or message.topic.endswith(
                            "/updates"
                        ):
                            receipt = dict(
                                schema_version=1,
                                type="MQTT_ACK",
                                consumer_id="backend-01",
                                publication_id=data["publication_id"],
                            )
                            client.publish(
                                "smartchair/v1/bridges/pi-01/receipts",
                                json.dumps(receipt).encode(),
                                qos=1,
                            )

                    observer.on_message = observe
                    observer.connect("127.0.0.1", port)
                    observer.loop_start()
                    self.assertTrue(subscribed.wait(5))

                    app_client = mqtt.Client(
                        mqtt.CallbackAPIVersion.VERSION2,
                        client_id="chair-test-app",
                        protocol=mqtt.MQTTv5,
                    )
                    settings = BridgeConfig(
                        node_id=NODE,
                        sensor_mac=MAC,
                        bridge_id="pi-01",
                        consumer_id="backend-01",
                        database_path=directory / "chair.sqlite",
                        allow_local_plaintext=True,
                    )
                    bridge = ChairMqttBridge(app_client, settings).attach()
                    # The application creates, connects, and runs the client.
                    app_client.connect("127.0.0.1", port, **bridge.connect_options())
                    app_client.loop_start()
                    end = time.monotonic() + 5
                    while not bridge.health().subscriptions_ready:
                        if time.monotonic() >= end:
                            self.fail(
                                f"facade subscriptions not ready: {bridge.health()}"
                            )
                        time.sleep(0.02)

                    self.assertTrue(bridge.publish(event(), source_mac=MAC))
                    command = dict(
                        schema_version=1,
                        expires_at_ms=int(time.time() * 1000) + 60000,
                        message=dict(
                            version=2,
                            type="CONFIG_SET",
                            node_id=NODE,
                            command_id=COMMAND,
                            revision=1,
                            heartbeat_s=900,
                            movement_batch_s=3,
                            movement_enabled=True,
                            radio_windows_enabled=True,
                            radio_interval_s=30,
                            radio_open_s=5,
                            radio_motion_tail_s=10,
                        ),
                    )
                    info = observer.publish(
                        "smartchair/v1/nodes/chair-01/commands/config",
                        json.dumps(command).encode(),
                        qos=1,
                        retain=False,
                    )
                    info.wait_for_publish(timeout=3)
                    notice = bridge.receive(timeout=5)
                    self.assertIsNotNone(notice)
                    self.assertEqual(notice.disposition, "queued")
                    self.assertEqual(notice.request.message.command_id, COMMAND)

                    hello = bridge.gateway_opened()
                    bridge.gateway_received(
                        dict(kind="hello", request_id=hello["request_id"])
                    )
                    bridge.publish(
                        event(
                            "READY",
                            seq=2,
                            reason="scheduled",
                            listen_remaining_ms=10000,
                        ),
                        source_mac=MAC,
                    )
                    # Reconnect clears stale ACKs; a sensor retransmission is ACKed again.
                    self.assertFalse(bridge.publish(event(), source_mac=MAC))
                    outgoing = bridge.gateway_next()
                    self.assertEqual(outgoing["kind"], "event_ack")
                    self.assertTrue(bridge.gateway_write(outgoing, io.BytesIO()))
                    bridge.gateway_received(
                        dict(kind="tx_result", request_id=outgoing["request_id"])
                    )
                    outgoing = bridge.gateway_next()
                    self.assertEqual(outgoing["message"]["type"], "READY_PROBE")
                    self.assertTrue(bridge.gateway_write(outgoing, io.BytesIO()))
                    bridge.gateway_received(
                        dict(kind="tx_result", request_id=outgoing["request_id"])
                    )
                    probe = outgoing["message"]
                    proof = event(
                        "READY_PROOF",
                        seq=3,
                        listen_remaining_ms=10000,
                        ack_boot_id=probe["boot_id"],
                        ack_seq=probe["seq"],
                    )
                    bridge.gateway_received(
                        dict(kind="telemetry", source_mac=MAC, message=proof)
                    )
                    outgoing = bridge.gateway_next()
                    self.assertEqual(outgoing["message"]["type"], "CONFIG_SET")
                    self.assertEqual(outgoing["message"]["command_id"], COMMAND)
                    self.assertTrue(bridge.gateway_write(outgoing, io.BytesIO()))
                    result = event(
                        "COMMAND_RESULT", seq=4, command_id=COMMAND, status="applied"
                    )
                    bridge.publish(result, source_mac=MAC)

                    seen = set()
                    end = time.monotonic() + 8
                    while time.monotonic() < end:
                        try:
                            topic, data = messages.get(timeout=0.2)
                        except queue.Empty:
                            continue
                        if topic.endswith("/events"):
                            seen.add(data["message"]["type"])
                        if (
                            topic.endswith("/status")
                            and data is not None
                            and data.get("type") == "COMMAND_STATUS"
                        ):
                            seen.add(data["status"])
                        if {"STATE_SYNC", "COMMAND_RESULT", "applied"} <= seen:
                            break
                    self.assertTrue(
                        {"STATE_SYNC", "COMMAND_RESULT", "applied"} <= seen, seen
                    )
                    end = time.monotonic() + 5
                    while True:
                        with closing(sqlite3.connect(directory / "chair.sqlite")) as db:
                            count = db.execute(
                                "SELECT count(*) FROM mqtt_outbox WHERE state!='completed'"
                            ).fetchone()[0]
                        if count == 0:
                            break
                        if time.monotonic() >= end:
                            self.fail(
                                f"{count} MQTT publications remain unacknowledged"
                            )
                        time.sleep(0.05)
                    # Advance only the saved terminal timestamp to test retention
                    # without waiting seven days or changing the system clock.
                    with closing(sqlite3.connect(directory / "chair.sqlite")) as db:
                        with db:
                            db.execute(
                                "UPDATE mqtt_commands SET terminal_ms=? WHERE id=?",
                                (int(time.time() * 1000) - 8 * 86400000, COMMAND),
                            )
                    bridge._coordinator.next_cleanup = 0
                    end = time.monotonic() + 5
                    while True:
                        with closing(sqlite3.connect(directory / "chair.sqlite")) as db:
                            state = db.execute(
                                "SELECT retention_state FROM mqtt_commands WHERE id=?",
                                (COMMAND,),
                            ).fetchone()[0]
                        if state == "deleted":
                            break
                        if time.monotonic() >= end:
                            self.fail(
                                f"retained command status was not removed: {state}"
                            )
                        time.sleep(0.05)

                    barrier = observer.publish(
                        "smartchair/v1/test/barrier", b'{"marker":"ready"}', qos=1
                    )
                    barrier.wait_for_publish(timeout=3)
                    end = time.monotonic() + 5
                    while time.monotonic() < end:
                        seen_topic, _ = messages.get(timeout=1)
                        if seen_topic.endswith("/test/barrier"):
                            break
                    else:
                        self.fail("observer barrier was not delivered")
                    observer.publish(
                        "smartchair/v1/nodes/chair-01/commands/config",
                        json.dumps(command).encode(),
                        qos=1,
                    )
                    duplicate = bridge.receive(timeout=5)
                    self.assertIsNotNone(duplicate)
                    self.assertEqual(duplicate.disposition, "duplicate")
                    status_topic = (
                        f"smartchair/v1/nodes/{NODE}/commands/{COMMAND}/status"
                    )
                    end = time.monotonic() + 5
                    while time.monotonic() < end:
                        seen_topic, data = messages.get(timeout=1)
                        if (
                            seen_topic == status_topic
                            and data
                            and data.get("status") == "applied"
                        ):
                            break
                    else:
                        self.fail("duplicate did not receive its non-retained status")

                    probe = mqtt.Client(
                        mqtt.CallbackAPIVersion.VERSION2,
                        client_id="chair-test-late-subscriber",
                        protocol=mqtt.MQTTv5,
                    )
                    probe_subscribed = threading.Event()
                    probe_messages = queue.Queue()
                    probe.on_subscribe = lambda *args: probe_subscribed.set()
                    probe.on_message = lambda client, userdata, message: (
                        probe_messages.put(message.payload)
                    )
                    try:
                        probe.connect("127.0.0.1", port)
                        probe.subscribe(status_topic, qos=1)
                        probe.loop_start()
                        self.assertTrue(probe_subscribed.wait(5))
                        with self.assertRaises(queue.Empty):
                            probe_messages.get(timeout=0.3)
                    finally:
                        probe.disconnect()
                        probe.loop_stop()

                    # The application owns reconnection. A fact accepted while the
                    # broker is down must be forwarded after the caller's loop reconnects.
                    subscribed.clear()
                    broker.terminate()
                    broker.wait(timeout=5)
                    end = time.monotonic() + 5
                    while bridge.health().mqtt_connected:
                        if time.monotonic() >= end:
                            self.fail("facade did not observe broker disconnection")
                        time.sleep(0.05)
                    bridge.publish(event("HEARTBEAT", seq=5), source_mac=MAC)
                    with closing(sqlite3.connect(directory / "chair.sqlite")) as db:
                        self.assertGreater(
                            db.execute(
                                "SELECT count(*) FROM mqtt_outbox WHERE state!='completed'"
                            ).fetchone()[0],
                            0,
                        )
                    broker = subprocess.Popen(
                        [BROKER, "-c", str(config_file)], stdout=log, stderr=log
                    )
                    end = time.monotonic() + 15
                    while not bridge.health().subscriptions_ready:
                        if time.monotonic() >= end:
                            self.fail(f"facade did not recover: {bridge.health()}")
                        time.sleep(0.05)
                    self.assertTrue(subscribed.wait(10))
                    end = time.monotonic() + 8
                    while time.monotonic() < end:
                        seen_topic, data = messages.get(timeout=1)
                        if (
                            seen_topic.endswith("/events")
                            and data
                            and data["message"]["type"] == "HEARTBEAT"
                        ):
                            break
                    else:
                        self.fail("offline heartbeat was not forwarded after reconnect")
                    self.assertTrue(bridge.detach())
                    bridge = None
                finally:
                    if bridge is not None:
                        bridge.detach()
                    for client in (app_client, observer):
                        if client is not None:
                            client.disconnect()
                            client.loop_stop()
                    if broker.poll() is None:
                        broker.terminate()
                        try:
                            broker.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            broker.kill()
                            broker.wait(timeout=5)
