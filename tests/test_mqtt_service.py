import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from chair_mqtt import service


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
            client.connect.assert_called_once_with("broker.example", 8883, keepalive=30)
            bridge.start_gateway.assert_called_once_with("/dev/gateway")
            client.disconnect.assert_called_once()
            client.loop_stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
