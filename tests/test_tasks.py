import os
import subprocess
import tempfile
import unittest
from pathlib import Path

TASKS = Path(__file__).resolve().parent.parent / "task-sets"
HOOKS_INSTALL = TASKS / "governance" / "hooks-install.sh"


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

    def test_writes_the_npm_then_system_channels_when_preferences_are_missing(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.preferences.read_text(), '[hook_preferences]\nchannels = ["npm", "system"]\n')
        self.assertEqual(self.calls.read_text(), "hooks install\n")

    def test_appends_the_table_after_other_preferences(self) -> None:
        self.preferences.write_text('[cache]\ndir = "x"\n')
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.preferences.read_text(), '[cache]\ndir = "x"\n\n[hook_preferences]\nchannels = ["npm", "system"]\n'
        )

    def test_keeps_preferences_that_already_allow_system(self) -> None:
        text = '[hook_preferences]\nchannels = ["user", "system"]\n'
        self.preferences.write_text(text)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.preferences.read_text(), text)

    def test_keeps_preferences_that_already_allow_npm(self) -> None:
        text = '[hook_preferences]\nchannels = ["npm"]\n'
        self.preferences.write_text(text)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.preferences.read_text(), text)

    def test_rejects_preferences_without_npm_or_system_even_if_another_table_has_them(self) -> None:
        self.preferences.write_text('[hook_preferences]\nchannels = ["npx"]\n[other]\nchannels = ["npm", "system"]\n')
        result = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertIn('Add "npm" to hook_preferences.channels', result.stderr)
        self.assertFalse(self.calls.exists())

    def test_installs_with_npx_and_no_ai_rulez(self) -> None:
        (self.bin / "ai-rulez").unlink()
        self._stub("npx", "")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls.read_text(), "hooks install\n")

    def test_fails_without_npx_or_ai_rulez(self) -> None:
        (self.bin / "ai-rulez").unlink()
        result = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Neither npx nor ai-rulez is on PATH", result.stderr)
        self.assertFalse(self.calls.exists())


class StubbedTaskTest(unittest.TestCase):
    """Runs a shared task in a scratch repository with stub tools on PATH."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "calls"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def stub(self, name: str, body: str = "") -> None:
        path = self.bin / name
        path.write_text(f'#!/bin/sh\necho "{name} $*" >> "{self.calls}"\n{body}\n')
        path.chmod(0o755)

    def run_task(self, task: str, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        full_env = {**os.environ, "PATH": f"{self.bin}:/usr/bin:/bin", **env}
        return subprocess.run(
            ["bash", str(TASKS / task), *args], cwd=self.root, env=full_env, capture_output=True, text=True, check=False
        )

    def called(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []


class AlefPinCheckTest(StubbedTaskTest):
    def setUp(self) -> None:
        super().setUp()
        (self.root / "alef.toml").write_text('alef_version = "0.115.5"\n')

    def test_accepts_the_pinned_clean_release(self) -> None:
        self.stub("alef", 'echo "alef 0.115.5"')
        self.assertEqual(self.run_task("alef-pin/pin-check.sh").returncode, 0)

    def test_rejects_another_version(self) -> None:
        self.stub("alef", 'echo "alef 0.115.4"')
        result = self.run_task("alef-pin/pin-check.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("pins 0.115.5 but the alef on PATH is 0.115.4", result.stderr)

    def test_rejects_a_dirty_build(self) -> None:
        self.stub("alef", 'echo "alef 0.115.5"; echo "tree: abc DIRTY"')
        self.assertEqual(self.run_task("alef-pin/pin-check.sh").returncode, 1)

    def test_rejects_a_missing_pin(self) -> None:
        (self.root / "alef.toml").write_text("")
        self.stub("alef", 'echo "alef 0.115.5"')
        self.assertIn("no alef_version pin", self.run_task("alef-pin/pin-check.sh").stderr)


class AlefTasksTest(StubbedTaskTest):
    def test_regenerate_runs_every_version_bearing_generator_in_order(self) -> None:
        self.stub("alef")
        self.assertEqual(self.run_task("alef/regenerate.sh").returncode, 0)
        self.assertEqual(
            self.called(),
            [
                "alef generate --skip-compile",
                "alef stubs",
                "alef scaffold",
                "alef readme",
                "alef test-apps generate",
                "alef e2e generate",
            ],
        )

    def test_verify_strict_fails_on_drifted_frozen_paths_despite_exit_zero(self) -> None:
        self.stub("alef", 'echo "2 frozen path(s) DRIFTED"')
        self.assertEqual(self.run_task("alef/verify-strict.sh").returncode, 1)

    def test_verify_strict_fails_when_verify_fails(self) -> None:
        self.stub("alef", "exit 3")
        self.assertNotEqual(self.run_task("alef/verify-strict.sh").returncode, 0)

    def test_verify_strict_passes_a_clean_tree(self) -> None:
        self.stub("alef", 'echo "all managed files up to date"')
        self.assertEqual(self.run_task("alef/verify-strict.sh").returncode, 0)


class CargoVersionTest(StubbedTaskTest):
    def test_prints_the_first_top_level_version(self) -> None:
        (self.root / "Cargo.toml").write_text(
            '[workspace.package]\nversion = "3.19.0"\n[dependencies]\nfoo = { version = "1.0" }\n'
        )
        result = self.run_task("alef-version/show.sh")
        self.assertEqual(result.stdout, "3.19.0\n")


class DepsRefreshTest(StubbedTaskTest):
    def setUp(self) -> None:
        super().setUp()
        self.stub("cargo")
        self.stub("go")
        (self.root / "packages" / "go").mkdir(parents=True)

    def test_rejects_an_unknown_mode(self) -> None:
        self.assertEqual(
            self.run_task("deps-refresh/refresh.sh", usage_mode="sideways", usage_lang="all").returncode, 2
        )

    def test_upgrade_runs_incompatible_upgrades_before_the_update(self) -> None:
        result = self.run_task("deps-refresh/refresh.sh", usage_mode="upgrade", usage_lang="rust")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called(), ["cargo upgrade --incompatible", "cargo update"])

    def test_all_follows_deps_langs_and_package_dirs(self) -> None:
        result = self.run_task("deps-refresh/refresh.sh", usage_mode="update", usage_lang="all", DEPS_LANGS="rust go")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called(), ["cargo update", "go get -u=patch ./...", "go mod tidy"])

    def test_zig_fetches_each_dependency_url_without_quotes_or_commas(self) -> None:
        self.stub("zig")
        (self.root / "packages" / "zig").mkdir(parents=True)
        (self.root / "packages" / "zig" / "build.zig.zon").write_text(
            '.dependencies = .{\n    .a = .{\n        .url = "https://example.com/a.tar.gz#abc",\n    },\n'
            '    .b = .{\n        .url = "https://example.com/b.tar.gz",\n    },\n}\n'
        )
        result = self.run_task("deps-refresh/refresh.sh", usage_mode="update", usage_lang="zig")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.called(),
            ["zig fetch --save https://example.com/a.tar.gz", "zig fetch --save https://example.com/b.tar.gz"],
        )

    def test_zig_with_no_pinned_url_succeeds_without_fetching(self) -> None:
        self.stub("zig")
        (self.root / "packages" / "zig").mkdir(parents=True)
        (self.root / "packages" / "zig" / "build.zig.zon").write_text(
            ".{\n    .name = .demo,\n    .dependencies = .{},\n}\n"
        )
        result = self.run_task("deps-refresh/refresh.sh", usage_mode="upgrade", usage_lang="zig")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nothing to refresh", result.stdout)
        self.assertEqual(self.called(), [])

    def test_all_requires_deps_langs(self) -> None:
        self.assertNotEqual(
            self.run_task("deps-refresh/refresh.sh", usage_mode="update", usage_lang="all").returncode, 0
        )


if __name__ == "__main__":
    unittest.main()
