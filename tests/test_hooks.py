import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".githooks/pre-commit"


class HookTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        self.git("init", "--quiet")
        (self.repo / "Makefile").write_text(
            "check:\n"
            '\t@test -z "$$GIT_INDEX_FILE"\n'
            '\t@test "$$(cat value.txt)" = good\n'
        )
        self.set_value("good")
        self.git("add", "Makefile", "value.txt")

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    def set_value(self, value):
        (self.repo / "value.txt").write_text(value + "\n")

    def run_hook(self):
        environment = dict(os.environ)
        environment["GIT_INDEX_FILE"] = str(self.repo / ".git/index")
        return subprocess.run(
            [str(HOOK)], cwd=self.repo, env=environment, capture_output=True, text=True
        )

    def test_checks_staged_content_and_preserves_unstaged_and_untracked_files(self):
        self.set_value("unstaged work")
        untracked = self.repo / "notes.txt"
        untracked.write_text("keep this\n")
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.repo / "value.txt").read_text(), "unstaged work\n")
        self.assertEqual(self.git("show", ":value.txt"), "good")
        self.assertEqual(untracked.read_text(), "keep this\n")

    def test_rejects_bad_staged_content_even_when_working_copy_is_good(self):
        self.set_value("bad")
        self.git("add", "value.txt")
        self.set_value("good")
        result = self.run_hook()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git("show", ":value.txt"), "bad")
        self.assertEqual((self.repo / "value.txt").read_text(), "good\n")

    def test_rejects_staged_whitespace_errors(self):
        self.set_value("good ")
        self.git("add", "value.txt")
        self.assertNotEqual(self.run_hook().returncode, 0)
        self.assertEqual(self.git("show", ":value.txt"), "good")
        self.assertEqual((self.repo / "value.txt").read_text(), "good \n")

    def test_setup_hooks_sets_repository_local_hook_path(self):
        result = subprocess.run(
            [
                "make",
                "--no-print-directory",
                "-f",
                str(ROOT / "Makefile"),
                "setup-hooks",
            ],
            cwd=self.repo,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.git("config", "--local", "--get", "core.hooksPath"), ".githooks"
        )


if __name__ == "__main__":
    unittest.main()
