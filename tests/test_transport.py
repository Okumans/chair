import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("transport", ROOT / "tools/transport.py")
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)


class TransportTests(unittest.TestCase):
    def test_binary_and_text(self):
        payload = bytes(range(250))
        message = transport.command("/hex " + payload.hex(), 1)
        self.assertEqual(transport.decode_hex(message["data_hex"]), payload)
        self.assertEqual(transport.command("hello chair", 2)["data_hex"], b"hello chair".hex())
        self.assertEqual(transport.command("เก้าอี้", 3)["data_hex"], "เก้าอี้".encode().hex())
        for invalid in ["", "x" * 251, "/hex 0", "/hex gg", "/hex 00 ff"]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                transport.command(invalid, 1)

    def test_framing_and_validation(self):
        lines = transport.Lines()
        self.assertEqual(lines.feed(b'{"kind":'), [])
        self.assertEqual(lines.feed(b'"hello"}\r\nnext\n'), [b'{"kind":"hello"}', b'next'])
        self.assertEqual(lines.feed(b'x' * (transport.MAX_LINE + 1)), [])
        self.assertEqual(lines.feed(b'\nvalid\n'), [b'valid'])
        self.assertEqual(transport.received(b'{"kind":"rx","data_hex":"00ff"}')["data_hex"], "00ff")
        self.assertEqual(transport.received(b'{"kind":"rx","data_hex":"6869"}')["text"], "hi")
        for invalid in [b'[]', b'{}', b'{', b'{"kind":"rx","data_hex":"zz"}']:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                transport.received(invalid)

    def test_native_codec(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "codec-test"
            subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            "-I", str(ROOT / "shared/include"),
                            str(ROOT / "tests/transport_codec_test.cpp"), "-o", str(binary)],
                           check=True)
            subprocess.run([str(binary)], check=True)

    def test_sensor_sleep(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "sensor-sleep-test"
            subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            "-I", str(ROOT / "tests/fakes"),
                            "-I", str(ROOT / "sensor/include"),
                            "-I", str(ROOT / "shared/include"),
                            str(ROOT / "tests/sensor_sleep_test.cpp"), "-o", str(binary)],
                           check=True)
            subprocess.run([str(binary)], check=True)
        self.assertEqual(transport.received(b'{"kind":"sleep","wake_pin":5}')["kind"], "sleep")

    def test_console_over_simulated_usb(self):
        master, slave = pty.openpty()
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "tools/transport.py"), os.ttyname(slave)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            self.assertTrue(select.select([master], [], [], 5)[0], "console did not open USB")
            self.assertEqual(json.loads(os.read(master, 2048)), {"kind": "hello"})
            # A real gateway forwards arbitrary peer bytes in this envelope.
            os.write(master, b'{"kind":"rx","source_mac":"02:00:00:00:00:01",')
            os.write(master, b'"data_hex":"000aff6869"}\n')
            self.assertTrue(select.select([process.stdout], [], [], 5)[0], "missing received packet")
            message = json.loads(process.stdout.readline())
            self.assertEqual(message["data_hex"], "000aff6869")
            self.assertNotIn("text", message)
            process.stdin.write(b'/hex 00ff010a\n')
            process.stdin.flush()
            self.assertTrue(select.select([master], [], [], 5)[0], "missing outgoing packet")
            message = json.loads(os.read(master, 2048))
            self.assertEqual(message, {"kind": "tx", "request_id": 1, "data_hex": "00ff010a"})
            os.write(master, b'{"kind":"tx_result","request_id":1,"mac_delivered":true}\n')
            self.assertTrue(select.select([process.stdout], [], [], 5)[0])
            self.assertTrue(json.loads(process.stdout.readline())["mac_delivered"])
            process.stdin.write(b'/quit\n')
            process.stdin.flush()
            self.assertEqual(process.wait(timeout=5), 0)
        finally:
            process.kill() if process.poll() is None else None
            process.communicate(timeout=5)
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    unittest.main()
