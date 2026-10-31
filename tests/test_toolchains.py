import io
import json
import subprocess
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
pnpm = "10.4.1"
python = "3.13.18"
go = "1.25.3"
ruby = "3.4.7"
java = "temurin-21.0.9+10.0.LTS"
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
        self.assertEqual(
            out.strip(),
            "checked 3 tools against the baseline: node, python, rust\nrepository-owned, not in the baseline: local-only",
        )

    def test_drifted_version_fails(self) -> None:
        self.write("mise.toml", '[tools]\nnode = "22.20.0"\n')

        code, _, err = self.run_main()

        self.assertEqual(code, 1)
        self.assertIn("drift: node: mise.toml pins 22.20.0, baseline is 24.22.1", err)

    def test_pinned_ai_rulez_fails_in_any_spelling(self) -> None:
        spellings = (
            '"github:Goldziher/ai-rulez"',
            '"npm:ai-rulez"',
            '"aqua:Goldziher/ai-rulez"',
            '"github:Goldziher/ai-rulez[bin=ai-rulez]"',
            '"npm:AI-Rulez"',
            '"cargo:ai_rulez"',
            '"go:github.com/Goldziher/ai-rulez/v5"',
        )
        for name in spellings:
            with self.subTest(name=name):
                self.write("mise.toml", f'[tools]\nnode = "24.22.1"\n{name} = "5.9.3"\n')

                code, out, err = self.run_main()

                self.assertEqual(code, 1)
                self.assertIn(
                    f"drift: {name.strip(chr(34))}: ai-rulez is never pinned; remove it from mise.toml and run "
                    "`npx -y ai-rulez@latest`",
                    err,
                )
                self.assertNotIn("repository-owned", out)

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

    def test_rust_in_mise_toml_is_rejected(self) -> None:
        self.write("mise.toml", '[tools]\nrust = "1.99.1"\n')

        code, _, err = self.run_main()

        self.assertEqual(code, 2)
        self.assertIn("declare rust in rust-toolchain.toml, not mise.toml", err)

    def test_alias_of_a_baseline_tool_is_drift_not_skipped(self) -> None:
        for key in ("nodejs", '"core:node"', '"aqua:nextest-rs/nextest"'):
            with self.subTest(key=key):
                self.write("mise.toml", f'[tools]\npython = "3.13.18"\n{key} = "20.0.0"\n')

                code, _, err = self.run_main()

                self.assertEqual(code, 1)
                self.assertIn("spelled differently from the baseline", err)

    def test_missing_table_options_are_drift(self) -> None:
        self.write("mise.toml", '[tools]\n"github:nextest-rs/nextest" = "0.9.149"\n')

        code, _, err = self.run_main()

        self.assertEqual(code, 1)
        self.assertIn("options {} differ from the baseline's {'version_prefix': 'cargo-nextest-'}", err)

    def test_root_taskfile_on_disk_fails(self) -> None:
        self.write("mise.toml", '[tools]\nnode = "24.22.1"\n')
        for name in ("Taskfile.yml", "taskfile.yaml", "Taskfile.dist.yml", "TASKFILE.DIST.YAML"):
            with self.subTest(name=name):
                self.write(name, "version: '3'\n")

                code, _, err = self.run_main()

                (self.repo / name).unlink()
                self.assertEqual(code, 1)
                self.assertIn(f"drift: {name}: go-task leftover", err)

    def test_root_task_directory_on_disk_fails(self) -> None:
        self.write("mise.toml", '[tools]\nnode = "24.22.1"\n')
        (self.repo / ".task").mkdir()

        code, _, err = self.run_main()

        self.assertEqual(code, 1)
        self.assertIn("drift: .task/: go-task leftover", err)

    def test_names_go_task_does_not_read_pass(self) -> None:
        self.write("mise.toml", '[tools]\nnode = "24.22.1"\n')
        self.write("Taskfile.yml.bak", "")
        (self.repo / "docs").mkdir()
        self.write("docs/Taskfile.yml", "")

        code, _, err = self.run_main()

        self.assertEqual(code, 0, err)

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def test_git_checkout_ignores_untracked_go_task_files(self) -> None:
        self.git("init", "-q")
        self.write("mise.toml", '[tools]\nnode = "24.22.1"\n')
        self.write("Taskfile.yml", "")
        (self.repo / ".task").mkdir()
        self.write(".task/checksum", "")
        self.git("add", "mise.toml")

        code, _, err = self.run_main()

        self.assertEqual(code, 0, err)

    def test_git_checkout_fails_on_tracked_go_task_files(self) -> None:
        self.git("init", "-q")
        self.write("mise.toml", '[tools]\nnode = "24.22.1"\n')
        self.write("Taskfile.yml", "")
        (self.repo / ".task").mkdir()
        self.write(".task/checksum", "")
        self.git("add", "mise.toml", "Taskfile.yml", ".task/checksum")
        (self.repo / "Taskfile.yml").unlink()

        code, _, err = self.run_main()

        self.assertEqual(code, 1)
        self.assertIn("drift: .task/: go-task leftover", err)
        self.assertIn("drift: Taskfile.yml: go-task leftover", err)


class VersionFilesTest(unittest.TestCase):
    """Version files mise does not read must agree with the baseline, or the repository's reasoned override."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.baseline = self.root / "baseline.toml"
        self.baseline.write_text(BASELINE)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.write("mise.toml", '[tools]\nnode = "24.22.1"\n')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, content: str) -> None:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def run_main(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = toolchains.main(["check", "--baseline", str(self.baseline), "--repo", str(self.repo)])
        return code, out.getvalue(), err.getvalue()

    def test_isolated_interpreter_runs_the_script(self) -> None:
        script = Path(toolchains.__file__).resolve()
        self.write(".nvmrc", "18\n")

        result = subprocess.run(
            [sys.executable, "-I", str(script), "check", "--baseline", str(self.baseline), "--repo", str(self.repo)],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("drift: node: .nvmrc pins 18, baseline is 24.22.1", result.stderr)

    def test_matching_version_files_pass_and_are_listed(self) -> None:
        files = {
            ".nvmrc": "v24.22.1\n",
            "docs/.node-version": "24.22.1",
            ".python-version": "# pyenv\n3.13.18\n3.12.1\n",
            ".ruby-version": "ruby-3.4.7\n",
            ".tool-versions": "# asdf\nnodejs 24.22.1\ngolang 1.25.3\nterraform 1.5.0\n",
            "package.json": '{"packageManager": "pnpm@10.4.1+sha512.abc"}',
            "e2e/rust-toolchain.toml": '[toolchain]\nchannel = "1.99.1"\n',
        }
        for name, content in files.items():
            self.write(name, content)

        code, out, err = self.run_main()

        self.assertEqual(code, 0, err)
        self.assertIn(f"checked {len(files)} version files: {', '.join(sorted(files))}", out)

    def test_drifted_version_files_fail(self) -> None:
        cases = {
            ".nvmrc": ("22.20.0\n", "node: .nvmrc pins 22.20.0, baseline is 24.22.1"),
            ".node-version": ("24\n", "node: .node-version pins 24, baseline is 24.22.1"),
            ".python-version": ("3.13\n", "python: .python-version pins 3.13, baseline is 3.13.18"),
            ".ruby-version": ("3.3.0", "ruby: .ruby-version pins 3.3.0, baseline is 3.4.7"),
            ".tool-versions": ("nodejs 22.20.0\n", "node: .tool-versions pins 22.20.0, baseline is 24.22.1"),
            "web/package.json": (
                '{\n  "packageManager": "pnpm@9.15.0"\n}\n',
                "pnpm: web/package.json packageManager pins 9.15.0, baseline is 10.4.1",
            ),
            "e2e/rust-toolchain.toml": (
                '[toolchain]\nchannel = "1.99"\n',
                "rust: e2e/rust-toolchain.toml pins 1.99, baseline is 1.99.1",
            ),
            "rust-toolchain": ("1.95.0\n", "rust: rust-toolchain pins 1.95.0, baseline is 1.99.1"),
        }
        for name, (content, expected) in cases.items():
            with self.subTest(name=name):
                self.write(name, content)

                code, _, err = self.run_main()

                (self.repo / name).unlink()
                self.assertEqual(code, 1)
                self.assertIn(f"drift: {expected}", err)

    def test_version_file_without_a_version_is_drift(self) -> None:
        cases = {
            ".nvmrc": ("\n", "node: .nvmrc names no version, baseline is 24.22.1"),
            "e2e/rust-toolchain.toml": (
                '[toolchain]\ncomponents = ["clippy"]\n',
                "rust: e2e/rust-toolchain.toml names no version, baseline is 1.99.1",
            ),
        }
        for name, (content, expected) in cases.items():
            with self.subTest(name=name):
                self.write(name, content)

                code, _, err = self.run_main()

                (self.repo / name).unlink()
                self.assertEqual(code, 1)
                self.assertIn(f"drift: {expected}", err)

    def test_override_applies_to_version_files(self) -> None:
        self.write("mise.toml", '[tools]\nnode = "22.20.0"\n')
        self.write("toolchains.toml", '[overrides.node]\nversion = "22.20.0"\nreason = "engine floor test"\n')
        self.write(".nvmrc", "22.20.0\n")

        code, _, err = self.run_main()

        self.assertEqual(code, 0, err)

        self.write(".nvmrc", "24.22.1\n")

        code, _, err = self.run_main()

        self.assertEqual(code, 1)
        self.assertIn("drift: node: .nvmrc pins 24.22.1, toolchains.toml override is 22.20.0", err)

    def test_vendored_and_installed_directories_are_skipped(self) -> None:
        self.write("node_modules/dep/package.json", '{"packageManager": "pnpm@8.0.0"}')
        self.write("vendor/lib/.nvmrc", "18\n")
        self.write("third_party/x/Dockerfile", "FROM node:18\n")
        self.write("package.json", '{"packageManager": "yarn@4.0.0"}')

        code, out, err = self.run_main()

        self.assertEqual(code, 0, err)
        self.assertNotIn("version files", out)

    def test_git_checkout_reads_tracked_files_only(self) -> None:
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True)
        self.write(".nvmrc", "18\n")
        self.write(".python-version", "3.13.18\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "mise.toml", ".python-version"], check=True)

        code, out, err = self.run_main()

        self.assertEqual(code, 0, err)
        self.assertIn("checked 1 version files: .python-version", out)

    def test_version_files_alone_count_as_checked(self) -> None:
        self.write("mise.toml", '[tools]\nlocal-only = "1.0"\n')
        self.write(".nvmrc", "24.22.1\n")

        code, _, err = self.run_main()

        self.assertEqual(code, 0, err)

    def test_dockerfile_tags_match_on_major_minor(self) -> None:
        self.write(
            "Dockerfile",
            "ARG PY=3.13\n"
            "FROM --platform=$BUILDPLATFORM node:24.22-slim AS web\n"
            "FROM docker.io/library/python:${PY}-slim-bookworm AS build\n"
            "from golang:1.25.0-alpine@sha256:abc\n"
            "FROM ruby:3.4\n"
            "FROM web\n"
            "FROM ghcr.io/acme/node:12\n"
            "FROM debian:bookworm\n",
        )

        code, out, err = self.run_main()

        self.assertEqual(code, 0, err)
        self.assertIn("checked 1 version files: Dockerfile", out)

    def test_drifted_dockerfile_tags_fail(self) -> None:
        cases = {
            "FROM golang:1.24-alpine\n": "go: Dockerfile FROM golang:1.24-alpine is 1.24, baseline is 1.25.3",
            "FROM node:24\n": "node: Dockerfile FROM node:24 names only a major; pin 24.22",
            "FROM node\n": "node: Dockerfile FROM node has no version tag; pin 24.22",
            "FROM python:latest\n": "python: Dockerfile FROM python:latest has no version tag; pin 3.13",
            "ARG V=3.3\nFROM ruby:$V-slim\n": "ruby: Dockerfile FROM ruby:3.3-slim is 3.3, baseline is 3.4.7",
        }
        for content, expected in cases.items():
            with self.subTest(content=content):
                self.write("docker/app.Dockerfile", content)

                code, _, err = self.run_main()

                self.assertEqual(code, 1)
                self.assertIn(f"drift: {expected.replace('Dockerfile FROM', 'docker/app.Dockerfile FROM')}", err)

    def test_unresolved_dockerfile_tag_is_reported_not_passed(self) -> None:
        self.write("Containerfile", "ARG NODE_VERSION\nFROM node:${NODE_VERSION}-slim\n")

        code, out, err = self.run_main()

        self.assertEqual(code, 0, err)
        self.assertIn("version files not checked: Containerfile (node:${NODE_VERSION}-slim)", out)
        self.assertNotIn("checked 1 version files", out)

    def test_nested_named_rust_channels_are_listed_not_checked(self) -> None:
        for channel in ("nightly", "nightly-2026-01-01", "stable", "beta"):
            with self.subTest(channel=channel):
                self.write("fuzz/rust-toolchain.toml", f'[toolchain]\nchannel = "{channel}"\n')
                self.write("miri/rust-toolchain", f"{channel}\n")

                code, out, err = self.run_main()

                self.assertEqual(code, 0, err)
                self.assertIn(
                    f"version files not checked: fuzz/rust-toolchain.toml (channel {channel}), "
                    f"miri/rust-toolchain (channel {channel})",
                    out,
                )

    def test_unparseable_rust_toolchain_toml_is_drift(self) -> None:
        self.write("e2e/rust-toolchain.toml", "[toolchain\nchannel = 1.99.1\n")

        code, _, err = self.run_main()

        self.assertEqual(code, 1)
        self.assertIn("drift: rust: e2e/rust-toolchain.toml is unparseable TOML", err)

    def test_tool_versions_skips_non_versions_and_non_temurin_java(self) -> None:
        self.write(".tool-versions", "nodejs system\npython latest\nruby lts\njava openjdk-21\n")

        code, out, err = self.run_main()

        self.assertEqual(code, 0, err)
        self.assertIn("version files not checked: .tool-versions (java openjdk-21)", out)

    def test_tool_versions_compares_temurin_java(self) -> None:
        self.write(".tool-versions", "java temurin-17.0.1+12\n")

        code, _, err = self.run_main()

        self.assertEqual(code, 1)
        self.assertIn("drift: java: .tool-versions pins temurin-17.0.1+12, baseline is temurin-21.0.9+10.0.LTS", err)

    def test_dockerfile_templates_and_docs_are_not_dockerfiles(self) -> None:
        for name in ("Dockerfile.j2", "Dockerfile.tmpl", "Dockerfile.md", "Dockerfile.txt", "Dockerfile.dev.j2"):
            self.write(name, "FROM node:12\n")
        self.write("Dockerfile.dev", "FROM node:24.22\n")

        code, out, err = self.run_main()

        self.assertEqual(code, 0, err)
        self.assertIn("checked 1 version files: Dockerfile.dev", out)

    def test_dockerfile_arg_fallbacks(self) -> None:
        self.write("Dockerfile", "ARG NODE\nFROM node:${NODE:-24.22}-slim\nFROM python:${PY:-3.12}\n")
        self.write("b.Dockerfile", "FROM node:${NODE:+24}\n")

        code, out, err = self.run_main()

        self.assertEqual(code, 1)
        self.assertNotIn("node: Dockerfile", err)
        self.assertIn("drift: python: Dockerfile FROM python:3.12 is 3.12, baseline is 3.13.18", err)
        self.assertIn("version files not checked: b.Dockerfile (node:${NODE:+24})", out)

    def test_failing_git_is_an_error_not_a_disk_walk(self) -> None:
        self.write(".git", "gitdir: /nonexistent/worktree\n")
        self.write(".nvmrc", "24.22.1\n")

        code, _, err = self.run_main()

        self.assertEqual(code, 2)
        self.assertIn("error: git cannot list the files of", err)

    def test_disk_walk_skips_build_outputs_and_environments(self) -> None:
        for directory in (".venv", "target", "dist", "build"):
            self.write(f"{directory}/.python-version", "3.8\n")

        code, out, err = self.run_main()

        self.assertEqual(code, 0, err)
        self.assertNotIn("version files", out)


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

    def test_single_quotes_header_comment_and_crlf_survive(self) -> None:
        (self.repo / "mise.toml").write_bytes(b"[tools] # pinned\r\nnode = '22.20.0'\r\n'python' = \"3.12.1\"\r\n")
        (self.repo / "rust-toolchain.toml").write_bytes(b"[toolchain]\r\n  channel = '1.95'\r\n")
        (self.repo / "toolchains.toml").write_text("baseline = 'v1.0.0'\n")

        code, out = self.sync("--ref", "v1.1.0")

        self.assertEqual(code, 0)
        self.assertEqual(out, "updated 3 pins: node, python, rust")
        self.assertEqual(
            (self.repo / "mise.toml").read_bytes(),
            b"[tools] # pinned\r\nnode = '24.22.1'\r\n'python' = \"3.13.18\"\r\n",
        )
        self.assertEqual((self.repo / "rust-toolchain.toml").read_bytes(), b"[toolchain]\r\n  channel = '1.99.1'\r\n")
        self.assertEqual((self.repo / "toolchains.toml").read_text(), 'baseline = "v1.1.0"\n')

    def test_unrewritable_entry_fails_and_writes_nothing(self) -> None:
        original = '[tools]\nnode = "22.20.0"\n\n[tools.python]\nversion = "3.12.1"\n'
        (self.repo / "mise.toml").write_text(original)
        (self.repo / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.95"\n')

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = toolchains.main(["sync", "--baseline", str(self.baseline), "--repo", str(self.repo), "--ref", "v2"])

        self.assertEqual(code, 2)
        self.assertIn("could not rewrite the pin for: python", err.getvalue())
        self.assertEqual((self.repo / "mise.toml").read_text(), original)
        self.assertEqual((self.repo / "rust-toolchain.toml").read_text(), '[toolchain]\nchannel = "1.95"\n')
        self.assertFalse((self.repo / "toolchains.toml").exists())

    def test_missing_table_options_are_not_silently_synced(self) -> None:
        original = '[tools]\n"github:nextest-rs/nextest" = "0.9.100"\n'
        (self.repo / "mise.toml").write_text(original)

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = toolchains.main(["sync", "--baseline", str(self.baseline), "--repo", str(self.repo)])

        self.assertEqual(code, 2)
        self.assertIn("copy the baseline's table options by hand for: github:nextest-rs/nextest", err.getvalue())
        self.assertEqual((self.repo / "mise.toml").read_text(), original)

    def test_repo_on_baseline_reports_no_change(self) -> None:
        (self.repo / "mise.toml").write_text('[tools]\nnode = "24.22.1"\n')

        code, out = self.sync()

        self.assertEqual(code, 0)
        self.assertEqual(out, "already on the baseline")
        self.assertFalse((self.repo / "toolchains.toml").exists())


class SyncVersionFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.baseline = self.root / "baseline.toml"
        self.baseline.write_text(BASELINE)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.write("mise.toml", '[tools]\nnode = "24.22.1"\n')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, content: str) -> None:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode())

    def read(self, name: str) -> str:
        return (self.repo / name).read_bytes().decode()

    def invoke(self, command: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = toolchains.main([command, "--baseline", str(self.baseline), "--repo", str(self.repo)])
        return code, out.getvalue(), err.getvalue()

    def test_sync_rewrites_single_version_files_then_check_passes(self) -> None:
        files = {
            ".nvmrc": ("v22.20.0\n", "v24.22.1\n"),
            "web/.node-version": ("24\r\n", "24.22.1\r\n"),
            ".python-version": ("# pyenv\n3.12.1  # main\n3.11.14\n", "# pyenv\n3.13.18  # main\n3.11.14\n"),
            ".ruby-version": ("ruby-3.3.0\n", "ruby-3.4.7\n"),
            "rust-toolchain.toml": ('[toolchain]\nchannel = "1.95.0"\n', '[toolchain]\nchannel = "1.99.1"\n'),
            "e2e/rust-toolchain.toml": (
                '[toolchain]\nchannel = "1.98"\ncomponents = ["clippy"]\n',
                '[toolchain]\nchannel = "1.99.1"\ncomponents = ["clippy"]\n',
            ),
            "legacy/rust-toolchain": ("1.90.0\n", "1.99.1\n"),
            "fuzz/rust-toolchain.toml": ('[toolchain]\nchannel = "nightly"\n', '[toolchain]\nchannel = "nightly"\n'),
            "package.json": (
                '{\n  "name": "x",\n  "packageManager": "pnpm@9.15.0+sha512.deadbeef"\n}\n',
                '{\n  "name": "x",\n  "packageManager": "pnpm@10.4.1"\n}\n',
            ),
            "docs/package.json": (
                '{"packageManager": "pnpm@10.4.1+sha512.keep"}',
                '{"packageManager": "pnpm@10.4.1+sha512.keep"}',
            ),
            "Dockerfile": ("FROM node:20-slim\n", "FROM node:20-slim\n"),
        }
        for name, (before, _) in files.items():
            self.write(name, before)

        code, out, err = self.invoke("sync")

        self.assertEqual((code, err), (0, ""))
        for name, (_, after) in files.items():
            with self.subTest(name=name):
                self.assertEqual(self.read(name), after)
        self.assertIn(
            "updated 7 version files: .nvmrc, .python-version, .ruby-version, e2e/rust-toolchain.toml, "
            "legacy/rust-toolchain, package.json, web/.node-version",
            out,
        )
        self.assertIn("updated 1 pins: rust", out)
        self.assertIn(
            "Dockerfile image tags are not rewritten; fix by hand: node: Dockerfile FROM node:20-slim names only a "
            "major; pin 24.22",
            out,
        )

        self.write("Dockerfile", "FROM node:24.22-slim\n")
        code, _, err = self.invoke("check")
        self.assertEqual(code, 0, err)

    def test_sync_writes_the_override_version(self) -> None:
        self.write("mise.toml", '[tools]\nnode = "22.20.0"\n')
        self.write("toolchains.toml", '[overrides.node]\nversion = "22.20.0"\nreason = "engine floor test"\n')
        self.write(".nvmrc", "24.22.1\n")

        code, _, _ = self.invoke("sync")

        self.assertEqual(code, 0)
        self.assertEqual(self.read(".nvmrc"), "22.20.0\n")

    def test_sync_on_matching_files_writes_nothing(self) -> None:
        self.write(".nvmrc", "24.22.1\n")
        before = (self.repo / ".nvmrc").stat().st_mtime_ns

        code, out, _ = self.invoke("sync")

        self.assertEqual(code, 0)
        self.assertNotIn("version files", out)
        self.assertEqual((self.repo / ".nvmrc").stat().st_mtime_ns, before)

    def test_failed_sync_leaves_version_files_alone(self) -> None:
        self.write("mise.toml", '[tools.node]\nversion = "22.20.0"\n')
        self.write(".nvmrc", "22.20.0\n")

        code, _, err = self.invoke("sync")

        self.assertEqual(code, 2)
        self.assertIn("could not rewrite the pin for: node", err)
        self.assertEqual(self.read(".nvmrc"), "22.20.0\n")


class RealBaselineTest(unittest.TestCase):
    def test_repository_baseline_parses_and_lists_tools(self) -> None:
        baseline = Path(__file__).resolve().parent.parent / "baseline.toml"
        tools = toolchains.load_toml(baseline)["tools"]

        versions = {name: toolchains.version_of(value, name) for name, value in tools.items()}

        self.assertRegex(versions["github:nextest-rs/nextest"], r"^\d+\.\d+\.\d+$")
        self.assertNotIn("task", versions)
        self.assertTrue(all(versions.values()))

    def test_renovate_preset_disables_exactly_the_baseline_tools(self) -> None:
        root = Path(__file__).resolve().parent.parent
        baseline = set(toolchains.load_toml(root / "baseline.toml")["tools"])
        rules = json.loads((root / "default.json").read_text())["packageRules"]

        disabled = [rule for rule in rules if rule.get("enabled") is False and rule.get("matchManagers") == ["mise"]]

        self.assertEqual(len(disabled), 1)
        self.assertEqual(set(disabled[0]["matchDepNames"]), baseline)

    def test_renovate_configs_leave_action_refs_to_gh_actions_updater(self) -> None:
        root = Path(__file__).resolve().parent.parent

        for name in ("default.json", "renovate.json"):
            with self.subTest(config=name):
                rules = json.loads((root / name).read_text())["packageRules"]
                actions = [rule for rule in rules if "github-actions" in rule.get("matchManagers", [])]

                self.assertEqual(len(actions), 1)
                self.assertEqual(actions[0]["matchManagers"], ["github-actions"])
                self.assertIs(actions[0].get("enabled"), False)
                self.assertEqual(set(actions[0]) - {"description"}, {"matchManagers", "enabled"})


if __name__ == "__main__":
    unittest.main()
