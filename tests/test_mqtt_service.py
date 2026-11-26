import json
import os
import pty
import select
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
from unittest.mock import Mock, patch

from chair_mqtt import service
from chair_mqtt.models import BridgeHealth

BROKER = shutil.which("mosquitto")


def health(**changes):
    values = dict(
        mqtt_connected=True,
        subscriptions_ready=True,
        gateway_connected=True,
        gateway_ready=True,
        storage_ok=True,
        clock_trusted=True,
        pending_publications=0,
        quarantined_publications=0,
        mqtt_best_effort_dropped=0,
        notifications_dropped=0,
        reconnect_required=False,
        fatal=None,
    )
    values.update(changes)
    return BridgeHealth(**values)


class ServiceTests(unittest.TestCase):
    def test_remote_plaintext_is_rejected_before_connecting(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bridge.toml"
            config.write_text(
                '[bridge]\nnode_id="chair-01"\n'
                'sensor_mac="02:00:00:00:00:01"\nbridge_id="pi-01"\n'
                'consumer_id="backend-01"\ndatabase_path="/tmp/test.sqlite"\n'
                '[mqtt]\nclient_id="pi-01"\nhost="broker.example"\n'
                '[gateway]\nport="/dev/gateway"\n'
            )
            with patch.object(service.mqtt, "Client") as client:
                with self.assertRaisesRegex(ValueError, "CA file"):
                    service.main(["--config", str(config)])
                client.assert_not_called()

    def test_service_connects_runs_gateway_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bridge.toml"
            config.write_text(
                '[bridge]\nnode_id="chair-01"\n'
                'sensor_mac="02:00:00:00:00:01"\nbridge_id="pi-01"\n'
                'consumer_id="backend-01"\ndatabase_path="/tmp/test.sqlite"\n'
                '[mqtt]\nclient_id="pi-01"\nhost="broker.example"\n'
                'ca_file="/ca.pem"\n[gateway]\nport="/dev/gateway"\n'
            )
            client = Mock()
            bridge = Mock()
            bridge.attach.return_value = bridge
            bridge.connect_options.return_value = {"keepalive": 30}
            bridge.detach.return_value = True
            event = Mock()
            event.wait.return_value = True
            with (
                patch.object(service.mqtt, "Client", return_value=client),
                patch.object(service, "ChairMqttBridge", return_value=bridge),
                patch.object(service.threading, "Event", return_value=event),
                patch.object(service.signal, "signal"),
                patch.dict(service.os.environ, {}, clear=True),
            ):
                service.main(["--config", str(config)])
            client.tls_set.assert_called_once_with(ca_certs="/ca.pem")
            client.connect_async.assert_called_once_with(
                "broker.example", 8883, keepalive=30
            )
            client.connect.assert_not_called()
            bridge.start_gateway.assert_called_once_with("/dev/gateway")
            client.disconnect.assert_called_once()
            client.loop_stop.assert_called_once()

    def run_with_health(self, states):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bridge.toml"
            config.write_text(
                '[bridge]\nnode_id="chair-01"\n'
                'sensor_mac="02:00:00:00:00:01"\nbridge_id="pi-01"\n'
                'consumer_id="backend-01"\ndatabase_path="/tmp/test.sqlite"\n'
                '[mqtt]\nclient_id="pi-01"\nhost="broker.example"\n'
                'ca_file="/ca.pem"\n[gateway]\nport="/dev/gateway"\n'
            )
            client = Mock()
            sock = client.socket.return_value
            bridge = Mock()
            bridge.attach.return_value = bridge
            bridge.connect_options.return_value = {}
            bridge.detach.return_value = True
            bridge.health.side_effect = states
            event = Mock()
            event.wait.return_value = False
            with (
                patch.object(service.mqtt, "Client", return_value=client),
                patch.object(service, "ChairMqttBridge", return_value=bridge),
                patch.object(service.threading, "Event", return_value=event),
                patch.object(service.signal, "signal"),
                patch.dict(service.os.environ, {}, clear=True),
                self.assertRaisesRegex(RuntimeError, "restart"),
            ):
                service.main(["--config", str(config)])
            return sock

    def test_reconnect_required_aborts_transport_once_and_keeps_running(self):
        sock = self.run_with_health(
            [
                health(reconnect_required=True),
                health(reconnect_required=True),
                health(mqtt_connected=False, reconnect_required=True),
                health(),
                health(fatal="MQTT subscription rejected"),
            ]
        )
        sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)

    def test_storage_failure_restarts_service(self):
        sock = self.run_with_health([health(storage_ok=False, reconnect_required=True)])
        sock.shutdown.assert_not_called()


@unittest.skipUnless(BROKER, "private Mosquitto broker unavailable")
class ServiceBrokerOutageTests(unittest.TestCase):
    def test_gateway_buffers_while_broker_is_down_then_publishes(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        database = directory / "bridge.sqlite"
        config = directory / "bridge.toml"
        config.write_text(
            '[bridge]\nnode_id="chair-01"\nsensor_mac="02:00:00:00:00:01"\n'
            'bridge_id="pi-01"\nconsumer_id="backend-01"\n'
            f'database_path="{database}"\nallow_local_plaintext=true\n'
            f'[mqtt]\nclient_id="pi-01"\nhost="127.0.0.1"\nport={port}\n'
            f'[gateway]\nport="{os.ttyname(slave)}"\n'
        )
        log = open(directory / "service.log", "wb")
        self.addCleanup(log.close)
        process = subprocess.Popen(
            [sys.executable, "-m", "chair_mqtt.service", "--config", str(config)],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        self.addCleanup(process.kill)
        pending = bytearray()

        def read_message(kind):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if b"\n" in pending:
                    line, _, rest = pending.partition(b"\n")
                    pending[:] = rest
                    value = json.loads(line)
                    if value["kind"] == kind:
                        return value
                elif select.select([master], [], [], 0.1)[0]:
                    pending.extend(os.read(master, 8192))
            self.fail(f"missing {kind}: {(directory / 'service.log').read_text()}")

        hello = read_message("hello")
        os.write(master, json.dumps(hello).encode() + b"\n")
        message = dict(
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
        )
        telemetry = dict(
            kind="telemetry", source_mac="02:00:00:00:00:01", message=message
        )
        os.write(master, json.dumps(telemetry).encode() + b"\n")
        self.assertEqual(read_message("event_ack")["message"]["ack_seq"], 1)
        self.assertIsNone(process.poll())

        conf = directory / "mosquitto.conf"
        conf.write_text(f"listener {port} 127.0.0.1\nallow_anonymous true\n")
        broker = subprocess.Popen(
            [BROKER, "-c", str(conf)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(broker.wait, timeout=5)
        self.addCleanup(broker.terminate)
        deadline = time.monotonic() + 30
        state = None
        while time.monotonic() < deadline and state != "waiting":
            time.sleep(0.2)
            with closing(sqlite3.connect(database)) as db:
                row = db.execute("SELECT state FROM mqtt_outbox").fetchone()
            state = row[0] if row else None
        self.assertEqual(state, "waiting")  # The broker acknowledged the event.

        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=15), 0)


if __name__ == "__main__":
    unittest.main()
