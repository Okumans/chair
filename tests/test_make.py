import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MakeMacTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name)
        self.log = path / "pio.json"
        pio = path / "pio"
        pio.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['MAC_TEST_PIO_LOG']).write_text(json.dumps(sys.argv[1:]))\n"
            "sys.exit(int(os.environ.get('MAC_TEST_PIO_EXIT', '0')))\n"
        )
        pio.chmod(0o755)
        self.environment = dict(os.environ)
        self.environment["PATH"] = str(path) + os.pathsep + self.environment["PATH"]
        self.environment["MAC_TEST_PIO_LOG"] = str(self.log)

    def run_make(self, environment, port):
        return subprocess.run(
            [
                "make",
                "--no-print-directory",
                "mac",
                f"ENV={environment}",
                f"PORT={port}",
            ],
            cwd=ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
        )

    def test_mac_uses_selected_board_and_same_upload_monitor_port(self):
        for board in ["sensor", "gateway"]:
            with self.subTest(board=board):
                result = self.run_make(board, "/tmp/board port")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads(self.log.read_text()),
                    [
                        "run",
                        "-e",
                        f"mac-{board}",
                        "-t",
                        "upload",
                        "-t",
                        "monitor",
                        "--port",
                        "/tmp/board port",
                    ],
                )

    def test_mac_rejects_missing_port_and_unknown_board_before_upload(self):
        for board, port in [("sensor", ""), ("other", "/dev/ttyACM1")]:
            with self.subTest(board=board, port=port):
                result = self.run_make(board, port)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.log.exists())

    def test_mac_propagates_platformio_failure(self):
        self.environment["MAC_TEST_PIO_EXIT"] = "1"
        result = self.run_make("gateway", "/dev/ttyACM1")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.log.exists())


if __name__ == "__main__":
    unittest.main()
