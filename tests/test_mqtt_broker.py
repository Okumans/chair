"""End-to-end bridge test against a private loopback MQTT broker."""

import json
import queue
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path

import paho.mqtt.client as mqtt
from gateway_fake import FakeGateway

from chair_mqtt import BridgeConfig, BrokerConfig, ChairMqttBridge

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
    def test_sensor_command_and_backend_receipts_through_a_broker(self):
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

                    settings = BridgeConfig(
                        node_id=NODE,
                        sensor_mac=MAC,
                        bridge_id="pi-01",
                        consumer_id="backend-01",
                        database_path=directory / "chair.sqlite",
                    )
                    broker_settings = BrokerConfig(
                        host="127.0.0.1",
                        port=port,
                        client_id="smartchair-pi-01",
                        plaintext=True,
                    )
                    gateway = FakeGateway()
                    bridge = ChairMqttBridge(
                        settings, broker_settings, gateway=gateway
                    ).start()
                    end = time.monotonic() + 5
                    while not bridge.health().subscriptions_ready:
                        if time.monotonic() >= end:
                            self.fail(
                                f"bridge subscriptions not ready: {bridge.health()}"
                            )
                        time.sleep(0.02)

                    self.assertEqual(
                        bridge.publish(event(), source_mac=MAC).status, "accepted"
                    )
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

                    gateway.send(gateway.expect("hello"))
                    end = time.monotonic() + 5
                    while not bridge.health().gateway_ready:
                        if time.monotonic() >= end:
                            self.fail("gateway handshake did not complete")
                        time.sleep(0.05)
                    bridge.publish(
                        event(
                            "READY",
                            seq=2,
                            reason="scheduled",
                            listen_remaining_ms=10000,
                        ),
                        source_mac=MAC,
                    )
                    # A radio retransmission of an accepted event is ACKed again.
                    gateway.send(
                        dict(kind="telemetry", source_mac=MAC, message=event())
                    )
                    outgoing = gateway.expect("event_ack")
                    self.assertEqual(outgoing["message"]["ack_seq"], 1)
                    gateway.send(
                        dict(kind="tx_result", request_id=outgoing["request_id"])
                    )
                    outgoing = gateway.expect("ready_probe")
                    self.assertEqual(outgoing["message"]["type"], "READY_PROBE")
                    gateway.send(
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
                    gateway.send(dict(kind="telemetry", source_mac=MAC, message=proof))
                    outgoing = gateway.expect("command")
                    self.assertEqual(outgoing["message"]["type"], "CONFIG_SET")
                    self.assertEqual(outgoing["message"]["command_id"], COMMAND)
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

                    # The bridge reconnects by itself. A fact accepted while the
                    # broker is down must be forwarded after it reconnects.
                    subscribed.clear()
                    broker.terminate()
                    broker.wait(timeout=5)
                    end = time.monotonic() + 5
                    while bridge.health().mqtt_connected:
                        if time.monotonic() >= end:
                            self.fail("bridge did not observe broker disconnection")
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
                            self.fail(f"bridge did not recover: {bridge.health()}")
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
                    # The broker acknowledged the offline status before DISCONNECT.
                    self.assertTrue(bridge.stop())
                    bridge = None
                finally:
                    if bridge is not None:
                        bridge.stop()
                    if observer is not None:
                        observer.disconnect()
                        observer.loop_stop()
                    if broker.poll() is None:
                        broker.terminate()
                        try:
                            broker.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            broker.kill()
                            broker.wait(timeout=5)
