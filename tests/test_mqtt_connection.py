"""Library-owned MQTT connections verify TLS before sending any MQTT bytes."""

import asyncio
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from pydantic import ValidationError

from chair_mqtt import (
    BridgeConfig,
    BrokerConfig,
    ChairClient,
    ChairMqttBridge,
    ClientConfig,
    SQLiteStorage,
)

OPENSSL = shutil.which("openssl")
CLIENT = ClientConfig(node_id="chair-01", bridge_id="pi-01", consumer_id="backend-01")


class BrokerConfigTests(unittest.TestCase):
    def test_plaintext_is_refused_for_a_remote_broker(self):
        with self.assertRaisesRegex(ValidationError, "loopback"):
            BrokerConfig(host="broker.example", client_id="backend", plaintext=True)
        BrokerConfig(host="127.0.0.1", client_id="backend", plaintext=True)

    def test_password_is_not_shown(self):
        broker = BrokerConfig(
            host="broker.example", client_id="backend", username="u", password="hunter2"
        )
        self.assertNotIn("hunter2", repr(broker))
        self.assertNotIn("hunter2", str(broker))


class TlsServer:
    """Accept TLS connections; record the first byte sent after each handshake."""

    def __init__(self, directory):
        self.cert, key = directory / "cert.pem", directory / "key.pem"
        subprocess.run(
            [OPENSSL, "req", "-x509", "-newkey", "ec", "-pkeyopt"]
            + ["ec_paramgen_curve:prime256v1", "-nodes", "-days", "1"]
            + ["-keyout", str(key), "-out", str(self.cert), "-subj", "/CN=localhost"]
            + ["-addext", "subjectAltName=DNS:localhost"],
            check=True,
            capture_output=True,
        )
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(self.cert, key)
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self.failures, self.received = 0, []
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stopping.is_set():
            try:
                raw, _ = self.listener.accept()
            except TimeoutError:
                continue
            with raw:
                raw.settimeout(2)
                try:
                    with self.context.wrap_socket(raw, server_side=True) as tls:
                        self.received.append(tls.recv(1))
                except OSError:
                    self.failures += 1

    def close(self):
        self.stopping.set()
        self.thread.join(5)
        self.listener.close()


@unittest.skipUnless(OPENSSL, "openssl unavailable")
class VerifiedTlsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.server = TlsServer(self.directory)
        self.addCleanup(self.server.close)

    async def attempt(self, **broker):
        chair = ChairClient(
            CLIENT,
            SQLiteStorage(self.directory / "backend.db"),
            BrokerConfig(port=self.server.port, client_id="backend", **broker),
        )
        async with chair:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not (
                self.server.failures or self.server.received
            ):
                await asyncio.sleep(0.05)
        return self.server.failures, self.server.received

    async def test_untrusted_certificate_receives_no_mqtt_bytes(self):
        failures, received = await self.attempt(host="localhost")
        self.assertGreater(failures, 0)
        self.assertEqual(received, [])

    async def test_host_name_must_match_the_certificate(self):
        failures, received = await self.attempt(
            host="127.0.0.1", ca_file=self.server.cert
        )
        self.assertGreater(failures, 0)
        self.assertEqual(received, [])

    async def test_trusted_certificate_and_host_receive_connect(self):
        failures, received = await self.attempt(
            host="localhost", ca_file=self.server.cert
        )
        self.assertEqual(received[:1], [b"\x10"])  # MQTT CONNECT


@unittest.skipUnless(OPENSSL, "openssl unavailable")
class BridgeVerifiedTlsTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.server = TlsServer(self.directory)
        self.addCleanup(self.server.close)

    def attempt(self, **broker):
        bridge = ChairMqttBridge(
            BridgeConfig(
                node_id="chair-01",
                sensor_mac="02:00:00:00:00:01",
                bridge_id="pi-01",
                consumer_id="backend-01",
                database_path=self.directory / "bridge.sqlite",
            ),
            BrokerConfig(port=self.server.port, client_id="smartchair-pi-01", **broker),
        )
        with bridge:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not (
                self.server.failures or self.server.received
            ):
                time.sleep(0.05)
        return self.server.failures, self.server.received

    def test_untrusted_certificate_receives_no_mqtt_bytes(self):
        failures, received = self.attempt(host="localhost")
        self.assertGreater(failures, 0)
        self.assertEqual(received, [])

    def test_trusted_certificate_and_host_receive_connect(self):
        failures, received = self.attempt(host="localhost", ca_file=self.server.cert)
        self.assertEqual(received[:1], [b"\x10"])  # MQTT CONNECT


if __name__ == "__main__":
    unittest.main()
