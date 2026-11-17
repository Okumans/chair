"""Black-box receipt/restart check using an automatically isolated loopback broker."""

import copy
import json
import os
import queue
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOSQUITTO = shutil.which("mosquitto") or next(
    (
        name
        for name in ("/usr/sbin/mosquitto", "/usr/local/sbin/mosquitto")
        if Path(name).is_file() and os.access(name, os.X_OK)
    ),
    None,
)


@unittest.skipUnless(MOSQUITTO, "mosquitto unavailable; no live broker is used")
class PrivateBrokerTests(unittest.TestCase):
    def test_durable_receipt_and_fresh_backend_process(self):
        import paho.mqtt.client as mqtt

        fixtures = json.loads((ROOT / "fixtures.json").read_text())
        event = next(x for x in fixtures if x["name"] == "state-sync-event")
        report = next(x for x in fixtures if x["name"] == "report-applied")
        with tempfile.TemporaryDirectory(prefix="mqtt-contract-test-") as directory:
            path = Path(directory)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            config = path / "mosquitto.conf"
            config.write_text(
                f"listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n"
            )
            with (path / "network.log").open("w+") as log:
                broker = subprocess.Popen(
                    [MOSQUITTO, "-c", str(config)],
                    stdout=log,
                    stderr=log,
                )
                worker = None
                client = None
                database = path / "backend.sqlite"
                received = queue.Queue()
                try:
                    end = time.monotonic() + 5
                    while True:
                        if broker.poll() is not None:
                            self.fail(
                                "private broker exited; no existing broker is contacted"
                            )
                        try:
                            with socket.create_connection(
                                ("127.0.0.1", port), timeout=0.1
                            ):
                                break
                        except OSError:
                            if time.monotonic() >= end:
                                self.fail("private broker did not start")
                            time.sleep(0.05)

                    def start_backend():
                        # Test identities and a fresh dedicated DB; no live service credentials.
                        env = dict(os.environ)
                        env.pop("MQTT_USERNAME", None)
                        env.pop("MQTT_PASSWORD", None)
                        return subprocess.Popen(
                            [
                                sys.executable,
                                str(ROOT / "backend_receipt_example.py"),
                                "--broker",
                                "127.0.0.1",
                                "--port",
                                str(port),
                                "--allow-local-plaintext",
                                "--site",
                                "lab",
                                "--consumer",
                                "backend-test",
                                "--bridge",
                                "pi-01",
                                "--db",
                                str(database),
                            ],
                            env=env,
                            stdout=log,
                            stderr=log,
                        )

                    worker = start_backend()
                    client = mqtt.Client(
                        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                        client_id="isolated-contract-observer",
                        protocol=mqtt.MQTTv5,
                    )
                    subscribed = queue.Queue()
                    client.on_subscribe = lambda *args: subscribed.put(True)
                    client.on_message = lambda c, u, m: received.put(
                        (json.loads(m.payload), m.properties)
                    )
                    client.connect("127.0.0.1", port)
                    client.subscribe("smartchair/v1/lab/bridges/pi-01/receipts", qos=1)
                    client.loop_start()
                    subscribed.get(timeout=5)

                    def publish_until_receipt(fixture):
                        end = time.monotonic() + 8
                        properties = mqtt.Properties(mqtt.PacketTypes.PUBLISH)
                        properties.PayloadFormatIndicator = 1
                        properties.ContentType = "application/json"
                        while time.monotonic() < end:
                            if worker.poll() is not None:
                                log.flush()
                                self.fail(
                                    "reference backend exited: "
                                    + (path / "network.log").read_text()[-2000:]
                                )
                            data = json.dumps(
                                fixture["payload"], separators=(",", ":")
                            ).encode()
                            info = client.publish(
                                fixture["topic"], data, qos=1, properties=properties
                            )
                            info.wait_for_publish(
                                timeout=2
                            )  # Test thread, never a network callback.
                            try:
                                ack, props = received.get(timeout=0.2)
                            except queue.Empty:
                                continue
                            if (
                                ack["publication_id"]
                                == fixture["payload"]["publication_id"]
                            ):
                                self.assertEqual(ack["consumer_id"], "backend-test")
                                self.assertEqual(ack["type"], "MQTT_ACK")
                                self.assertEqual(props.ContentType, "application/json")
                                self.assertEqual(props.PayloadFormatIndicator, 1)
                                return
                        self.fail("no durable receipt from isolated backend")

                    publish_until_receipt(event)
                    with closing(sqlite3.connect(database)) as db:
                        self.assertEqual(
                            db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0],
                            1,
                        )
                    # Exercise an oversized QoS-1 publication on the actual broker.
                    oversized = client.publish(event["topic"], b"x" * 16385, qos=1)
                    oversized.wait_for_publish(timeout=2)
                    end = time.monotonic() + 5
                    while True:
                        with closing(sqlite3.connect(database)) as db:
                            rejected = db.execute(
                                "SELECT COUNT(*) FROM rejected"
                            ).fetchone()[0]
                        if rejected:
                            break
                        if worker.poll() is not None or time.monotonic() >= end:
                            self.fail("oversized publication was not durably rejected")
                        time.sleep(0.05)
                    # A distinct valid report must still be delivered by the same process.
                    publish_until_receipt(copy.deepcopy(report))
                    worker.send_signal(signal.SIGINT)
                    worker.wait(timeout=5)
                    worker = (
                        start_backend()
                    )  # Same broker/session identity, fresh Paho object.
                    # A new report proves the new process subscribed and returned a fresh
                    # receipt before checking duplicate receipt regeneration. Drain any
                    # buffered old receipts so they cannot make the restart test pass.
                    fresh_report = copy.deepcopy(report)
                    fresh_report["payload"]["publication_id"] = (
                        "6c19f0de-eeb6-4c3a-9d8b-d029000a4405"
                    )
                    fresh_report["payload"]["report_seq"] += 1
                    publish_until_receipt(fresh_report)
                    while not received.empty():
                        received.get_nowait()
                    publish_until_receipt(event)
                    with closing(sqlite3.connect(database)) as db:
                        self.assertEqual(
                            db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0],
                            3,
                        )
                        self.assertEqual(
                            db.execute("SELECT COUNT(*) FROM facts").fetchone()[0], 3
                        )
                finally:
                    if client:
                        client.disconnect()
                        client.loop_stop()
                    for process in (worker, broker):
                        if process is not None and process.poll() is None:
                            process.terminate()
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
