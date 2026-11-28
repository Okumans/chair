import json
import subprocess
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

from pydantic import BaseModel

import chair_mqtt

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

    def test_model_schema_unchanged(self):
        """Typing must not change validation: compare every exported model's schema."""
        expected = json.loads((ROOT / "tests/data/model_schemas.json").read_text())
        actual = {}
        for name in chair_mqtt.__all__:
            value = getattr(chair_mqtt, name)
            if isinstance(value, type) and issubclass(value, BaseModel):
                actual[name] = value.model_json_schema()
        self.assertEqual(sorted(actual), sorted(expected))
        for name, schema in expected.items():
            with self.subTest(model=name):
                self.assertEqual(actual[name], schema)


if __name__ == "__main__":
    unittest.main()
