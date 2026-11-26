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
from unittest.mock import MagicMock, Mock, patch

from pydantic import ValidationError

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


BRIDGE = (
    '[bridge]\nnode_id="chair-01"\n'
    'sensor_mac="02:00:00:00:00:01"\nbridge_id="pi-01"\n'
    'consumer_id="backend-01"\ndatabase_path="/tmp/test.sqlite"\n'
    '[gateway]\nport="/dev/gateway"\n'
)
TLS_BROKER = (
    '[mqtt]\nclient_id="smartchair-pi-01"\nhost="broker.example"\nca_file="/ca.pem"\n'
)


class ServiceTests(unittest.TestCase):
    def run_service(self, mqtt=TLS_BROKER, *, environ=None, states=None):
        """Run main() with a mocked bridge; return the bridge class mock."""
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bridge.toml"
            config.write_text(BRIDGE + mqtt)
            bridge = MagicMock()
            bridge.__enter__.return_value = bridge
            if states is not None:
                bridge.health.side_effect = states
            event = Mock()
            event.wait.return_value = states is None
            with (
                patch.object(service, "ChairMqttBridge", return_value=bridge) as cls,
                patch.object(service.threading, "Event", return_value=event),
                patch.object(service.signal, "signal"),
                patch.dict(service.os.environ, environ or {}, clear=True),
            ):
                service.main(["--config", str(config)])
            return cls

    def test_remote_plaintext_is_rejected_before_a_bridge_exists(self):
        plaintext = '[mqtt]\nclient_id="x"\nhost="broker.example"\nplaintext=true\n'
        with (
            patch.object(service, "ChairMqttBridge") as cls,
            self.assertRaisesRegex(ValidationError, "loopback"),
        ):
            self.run_service(plaintext)
        cls.assert_not_called()

    def test_service_runs_bridge_and_gateway_until_signalled(self):
        cls = self.run_service(
            environ={"CHAIR_MQTT_USERNAME": "pi", "CHAIR_MQTT_PASSWORD": "secret"}
        )
        config, broker = cls.call_args.args
        self.assertEqual(config.bridge_id, "pi-01")
        self.assertEqual((broker.host, broker.port), ("broker.example", 8883))
        self.assertEqual(broker.ca_file, Path("/ca.pem"))
        self.assertEqual(broker.username, "pi")
        self.assertEqual(broker.password.get_secret_value(), "secret")
        bridge = cls.return_value
        bridge.start_gateway.assert_called_once_with("/dev/gateway")
        bridge.__exit__.assert_called_once()

    def test_credentials_in_the_configuration_file_are_refused(self):
        with self.assertRaisesRegex(ValueError, "CHAIR_MQTT_USERNAME"):
            self.run_service(TLS_BROKER + 'username="pi"\n')

    def test_fatal_bridge_restarts_service(self):
        with self.assertRaisesRegex(RuntimeError, "restart"):
            self.run_service(
                states=[health(), health(fatal="MQTT subscription rejected")]
            )

    def test_storage_failure_restarts_service(self):
        with self.assertRaisesRegex(RuntimeError, "restart"):
            self.run_service(states=[health(storage_ok=False)])


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
            f'database_path="{database}"\n'
            f'[mqtt]\nclient_id="pi-01"\nhost="127.0.0.1"\nport={port}\n'
            "plaintext=true\n"
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
