import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HOOKS_INSTALL = Path(__file__).resolve().parent.parent / "tasks" / "governance" / "hooks-install.sh"


class HooksInstallTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "calls"
        self._stub("poly", f'echo "$*" >> "{self.calls}"')
        self._stub("ai-rulez", "")
        self.preferences = self.root / "poly.local.toml"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)

    def _run(self, path: str | None = None) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PATH": path or f"{self.bin}:/usr/bin:/bin"}
        return subprocess.run(
            ["bash", str(HOOKS_INSTALL)], cwd=self.root, env=env, capture_output=True, text=True, check=False
        )

    def test_writes_the_system_channel_when_preferences_are_missing(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.preferences.read_text(), '[hook_preferences]\nchannels = ["system"]\n')
        self.assertEqual(self.calls.read_text(), "hooks install\n")

    def test_appends_the_table_after_other_preferences(self) -> None:
        self.preferences.write_text('[cache]\ndir = "x"\n')
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.preferences.read_text(), '[cache]\ndir = "x"\n\n[hook_preferences]\nchannels = ["system"]\n'
        )

    def test_keeps_preferences_that_already_allow_system(self) -> None:
        text = '[hook_preferences]\nchannels = ["user", "system"]\n'
        self.preferences.write_text(text)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.preferences.read_text(), text)

    def test_rejects_preferences_without_system_even_if_another_table_has_it(self) -> None:
        self.preferences.write_text('[hook_preferences]\nchannels = ["user"]\n[other]\nchannels = ["system"]\n')
        result = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertIn('Add "system" to hook_preferences.channels', result.stderr)
        self.assertFalse(self.calls.exists())

    def test_fails_without_ai_rulez(self) -> None:
        (self.bin / "ai-rulez").unlink()
        result = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("ai-rulez is not on PATH", result.stderr)
        self.assertFalse(self.calls.exists())


if __name__ == "__main__":
    unittest.main()
