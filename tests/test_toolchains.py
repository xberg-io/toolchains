import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import toolchains  # noqa: E402

BASELINE = """
[tools]
node = "24.22.1"
python = "3.13.18"
rust = "1.99.1"
"github:nextest-rs/nextest" = { version = "0.9.149", version_prefix = "cargo-nextest-" }
"""


class CheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.baseline = self.root / "baseline.toml"
        self.baseline.write_text(BASELINE)
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, content: str) -> None:
        (self.repo / name).write_text(content)

    def run_main(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = toolchains.main(["check", "--baseline", str(self.baseline), "--repo", str(self.repo)])
        return code, out.getvalue(), err.getvalue()

    def test_matching_repo_passes_and_reports_checked_tools(self) -> None:
        self.write("mise.toml", '[tools]\nnode = "24.22.1"\npython = ["3.13.18", "3.11.14"]\nlocal-only = "1.0"\n')
        self.write("rust-toolchain.toml", '[toolchain]\nchannel = "1.99.1"\n')

        code, out, err = self.run_main()

        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "checked 3 tools against the baseline: node, python, rust")

    def test_drifted_version_fails(self) -> None:
        self.write("mise.toml", '[tools]\nnode = "22.20.0"\n')

        code, _, err = self.run_main()

        self.assertEqual(code, 1)
        self.assertIn("drift: node: mise.toml pins 22.20.0, baseline is 24.22.1", err)

    def test_rust_channel_drift_fails(self) -> None:
        self.write("mise.toml", '[tools]\nnode = "24.22.1"\n')
        self.write("rust-toolchain.toml", '[toolchain]\nchannel = "1.95"\n')

        code, _, err = self.run_main()

        self.assertEqual(code, 1)
        self.assertIn("drift: rust: rust-toolchain.toml pins 1.95, baseline is 1.99.1", err)

    def test_table_entry_compares_its_version(self) -> None:
        self.write("mise.toml", '[tools]\n"github:nextest-rs/nextest" = { version = "0.9.100" }\n')

        code, _, err = self.run_main()

        self.assertEqual(code, 1)
        self.assertIn("github:nextest-rs/nextest: mise.toml pins 0.9.100, baseline is 0.9.149", err)

    def test_reasoned_override_passes(self) -> None:
        self.write("mise.toml", '[tools]\nnode = "22.20.0"\n')
        self.write("toolchains.toml", '[overrides.node]\nversion = "22.20.0"\nreason = "engine floor test"\n')

        code, _, err = self.run_main()

        self.assertEqual(code, 0, err)

    def test_override_problems_fail(self) -> None:
        cases = {
            '[overrides.node]\nversion = "22.20.0"\n': "node: override in toolchains.toml has no reason",
            '[overrides.node]\nversion = "24.22.1"\nreason = "x"\n': "node: override matches the baseline",
            '[overrides.node]\nversion = "20.0.0"\nreason = "x"\n': "node: override says 20.0.0, mise.toml pins",
            '[overrides.go]\nversion = "1.0"\nreason = "x"\n': "go: override in toolchains.toml for a tool the",
        }
        for overrides, expected in cases.items():
            with self.subTest(expected=expected):
                pinned = "24.22.1" if "24.22.1" in overrides else "22.20.0"
                self.write("mise.toml", f'[tools]\nnode = "{pinned}"\n')
                self.write("toolchains.toml", overrides)

                code, _, err = self.run_main()

                self.assertEqual(code, 1)
                self.assertIn(expected, err)

    def test_nothing_covered_is_an_error_not_a_pass(self) -> None:
        self.write("mise.toml", '[tools]\nlocal-only = "1.0"\n')

        code, _, err = self.run_main()

        self.assertEqual(code, 2)
        self.assertIn("nothing was checked", err)

    def test_missing_mise_toml_is_an_error(self) -> None:
        code, _, err = self.run_main()

        self.assertEqual(code, 2)
        self.assertIn("mise.toml: not found", err)

    def test_rust_disagreement_between_files_is_an_error(self) -> None:
        self.write("mise.toml", '[tools]\nrust = "1.99.1"\n')
        self.write("rust-toolchain.toml", '[toolchain]\nchannel = "1.95"\n')

        code, _, err = self.run_main()

        self.assertEqual(code, 2)
        self.assertIn("rust is 1.99.1 in mise.toml but 1.95 in rust-toolchain.toml", err)


class SyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.baseline = self.root / "baseline.toml"
        self.baseline.write_text(BASELINE)
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def sync(self, *extra: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = toolchains.main(["sync", "--baseline", str(self.baseline), "--repo", str(self.repo), *extra])
        self.assertEqual(err.getvalue(), "")
        return code, out.getvalue().strip()

    def test_rewrites_every_entry_form_and_keeps_layout(self) -> None:
        (self.repo / "mise.toml").write_text(
            "[settings]\n"
            'node = "not-a-tool"\n'
            "\n"
            "[tools]\n"
            "# runtime\n"
            'node = "22.20.0"  # keep me\n'
            'python = ["3.12.1", "3.11.14"]\n'
            '"github:nextest-rs/nextest" = { version = "0.9.100", version_prefix = "cargo-nextest-" }\n'
            'local-only = "1.0"\n'
        )
        (self.repo / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.95"\ncomponents = ["clippy"]\n')

        code, out = self.sync("--ref", "v1.2.0")

        self.assertEqual(code, 0)
        self.assertEqual(out, "updated 4 pins: github:nextest-rs/nextest, node, python, rust")
        self.assertEqual(
            (self.repo / "mise.toml").read_text(),
            "[settings]\n"
            'node = "not-a-tool"\n'
            "\n"
            "[tools]\n"
            "# runtime\n"
            'node = "24.22.1"  # keep me\n'
            'python = ["3.13.18", "3.11.14"]\n'
            '"github:nextest-rs/nextest" = { version = "0.9.149", version_prefix = "cargo-nextest-" }\n'
            'local-only = "1.0"\n',
        )
        self.assertEqual(
            (self.repo / "rust-toolchain.toml").read_text(),
            '[toolchain]\nchannel = "1.99.1"\ncomponents = ["clippy"]\n',
        )
        self.assertEqual((self.repo / "toolchains.toml").read_text(), 'baseline = "v1.2.0"\n')
        self.assertEqual(toolchains.check(self.baseline, self.repo).errors, [])

    def test_overridden_tool_is_left_alone(self) -> None:
        (self.repo / "mise.toml").write_text('[tools]\nnode = "22.20.0"\npython = "3.12.1"\n')
        (self.repo / "toolchains.toml").write_text(
            'baseline = "v1.0.0"\n\n[overrides.node]\nversion = "22.20.0"\nreason = "engine floor test"\n'
        )

        code, out = self.sync("--ref", "v1.1.0")

        self.assertEqual(code, 0)
        self.assertEqual(out, "updated 1 pins: python")
        self.assertEqual((self.repo / "mise.toml").read_text(), '[tools]\nnode = "22.20.0"\npython = "3.13.18"\n')
        self.assertTrue((self.repo / "toolchains.toml").read_text().startswith('baseline = "v1.1.0"\n'))

    def test_repo_on_baseline_reports_no_change(self) -> None:
        (self.repo / "mise.toml").write_text('[tools]\nnode = "24.22.1"\n')

        code, out = self.sync()

        self.assertEqual(code, 0)
        self.assertEqual(out, "already on the baseline")
        self.assertFalse((self.repo / "toolchains.toml").exists())


class RealBaselineTest(unittest.TestCase):
    def test_repository_baseline_parses_and_lists_tools(self) -> None:
        baseline = Path(__file__).resolve().parent.parent / "baseline.toml"
        tools = toolchains.load_toml(baseline)["tools"]

        versions = {name: toolchains.version_of(value, name) for name, value in tools.items()}

        self.assertEqual(versions["task"], "3.54.0")
        self.assertTrue(all(versions.values()))

    def test_renovate_preset_disables_exactly_the_baseline_tools(self) -> None:
        root = Path(__file__).resolve().parent.parent
        baseline = set(toolchains.load_toml(root / "baseline.toml")["tools"])
        rules = json.loads((root / "default.json").read_text())["packageRules"]

        disabled = [rule for rule in rules if rule.get("enabled") is False and rule.get("matchManagers") == ["mise"]]

        self.assertEqual(len(disabled), 1)
        self.assertEqual(set(disabled[0]["matchDepNames"]), baseline)


if __name__ == "__main__":
    unittest.main()
