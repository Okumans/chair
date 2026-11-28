import subprocess
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_wheel_ships_py_typed(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(
                [
                    "uv",
                    "build",
                    "--wheel",
                    "--out-dir",
                    directory,
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            wheels = sorted(Path(directory).glob("chair_mqtt-*.whl"))
            self.assertEqual(len(wheels), 1)
            with zipfile.ZipFile(wheels[0]) as wheel:
                names = wheel.namelist()
        self.assertIn("chair_mqtt/py.typed", names)
        self.assertIn("chair_mqtt/mqtt.schema.json", names)

    def test_pyright_targets_311(self):
        with (ROOT / "pyproject.toml").open("rb") as handle:
            pyproject = tomllib.load(handle)
        pyright = pyproject["tool"]["pyright"]
        self.assertEqual(pyright["pythonVersion"], "3.11")
        self.assertEqual(pyright["typeCheckingMode"], "strict")
        self.assertIn("tests", pyright["exclude"])


if __name__ == "__main__":
    unittest.main()
