import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/release_version.py"
sys.path.insert(0, str(ROOT / "tools"))

from release_version import tag_matches  # noqa: E402


class ReleaseVersionTests(unittest.TestCase):
    def test_match(self):
        self.assertTrue(tag_matches("v1.0", "1.0.0"))
        self.assertTrue(tag_matches("v1.0.0", "1.0.0"))
        self.assertTrue(tag_matches("v2", "2.0.0"))

    def test_mismatch(self):
        self.assertFalse(tag_matches("v1.1", "1.0.0"))
        self.assertFalse(tag_matches("v1.0.1", "1.0.0"))

    def test_malformed(self):
        for tag in ("1.0", "v1.0-rc1", "vfoo", ""):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                tag_matches(tag, "1.0.0")

    def test_cli_reads_pyproject(self):
        def run(tag):
            return subprocess.run(
                [sys.executable, str(SCRIPT), tag], capture_output=True, text=True
            )

        self.assertEqual(run("v1.0").returncode, 0)
        mismatch = run("v9.9")
        self.assertEqual(mismatch.returncode, 1)
        self.assertIn("does not match", mismatch.stderr)
        malformed = run("release-1")
        self.assertEqual(malformed.returncode, 1)
        self.assertIn("malformed tag", malformed.stderr)


if __name__ == "__main__":
    unittest.main()
