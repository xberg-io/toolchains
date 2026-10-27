#!/usr/bin/env python3
"""Keep a repository's mise toolchain on the org baseline.

`check` reads <repo>/mise.toml, <repo>/rust-toolchain.toml and <repo>/toolchains.toml. Every tool the repository
declares that the baseline also lists must use the baseline version, unless toolchains.toml records an
override with a reason. Exits 0 when consistent, 1 on drift, 2 on unusable input.

`sync` rewrites those pins to the baseline versions in place, skipping overridden tools, and records the
baseline ref in toolchains.toml. It edits matching lines only, so comments and layout survive.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import tomllib

RUST = "rust"


@dataclass
class Report:
    checked: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class InputError(Exception):
    pass


def load_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise InputError(f"{path}: invalid TOML: {exc}") from exc


def version_of(value: object, where: str) -> str:
    """Return the default version of a mise tool entry: a string, a list (first entry) or a table."""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        return version_of(value[0], where)
    if isinstance(value, dict) and isinstance(value.get("version"), str):
        return value["version"]
    raise InputError(f"{where}: cannot read a version from {value!r}")


def repo_versions(repo: Path) -> dict[str, tuple[str, str]]:
    """Map tool name to (version, source file) for everything the repository pins."""
    mise_path = repo / "mise.toml"
    if not mise_path.is_file():
        raise InputError(f"{mise_path}: not found")
    tools = load_toml(mise_path).get("tools", {})
    versions = {name: (version_of(value, f"{mise_path} [tools].{name}"), "mise.toml") for name, value in tools.items()}

    rust_path = repo / "rust-toolchain.toml"
    if rust_path.is_file():
        channel = load_toml(rust_path).get("toolchain", {}).get("channel")
        if not isinstance(channel, str):
            raise InputError(f"{rust_path}: [toolchain].channel missing")
        if RUST in versions and versions[RUST][0] != channel:
            raise InputError(f"rust is {versions[RUST][0]} in mise.toml but {channel} in rust-toolchain.toml")
        versions[RUST] = (channel, "rust-toolchain.toml")
    return versions


def load_overrides(repo: Path) -> dict[str, dict]:
    path = repo / "toolchains.toml"
    if not path.is_file():
        return {}
    overrides = load_toml(path).get("overrides", {})
    for name, entry in overrides.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("version"), str):
            raise InputError(f"{path}: [overrides.{name}] needs a version string")
    return overrides


def load_baseline(baseline_path: Path) -> dict[str, str]:
    baseline = {
        name: version_of(value, f"{baseline_path} [tools].{name}")
        for name, value in load_toml(baseline_path).get("tools", {}).items()
    }
    if not baseline:
        raise InputError(f"{baseline_path}: [tools] is empty")
    return baseline


def check(baseline_path: Path, repo: Path) -> Report:
    baseline = load_baseline(baseline_path)
    versions = repo_versions(repo)
    overrides = load_overrides(repo)
    report = Report()

    for name, (version, source) in sorted(versions.items()):
        if name not in baseline:
            continue
        report.checked.append(name)
        expected = baseline[name]
        override = overrides.get(name)
        if override is None:
            if version != expected:
                report.errors.append(f"{name}: {source} pins {version}, baseline is {expected}")
            continue
        if not str(override.get("reason", "")).strip():
            report.errors.append(f"{name}: override in toolchains.toml has no reason")
        elif override["version"] == expected:
            report.errors.append(f"{name}: override matches the baseline ({expected}); remove it")
        elif override["version"] != version:
            report.errors.append(f"{name}: override says {override['version']}, {source} pins {version}")

    for name in sorted(set(overrides) - set(versions)):
        report.errors.append(f"{name}: override in toolchains.toml for a tool the repository does not pin")
    for name in sorted(set(overrides) & set(versions) - set(baseline)):
        report.errors.append(f"{name}: override in toolchains.toml for a tool outside the baseline")
    return report


def tool_line_pattern(name: str) -> re.Pattern[str]:
    """Match a [tools] entry's default version: `name = "v"`, `name = ["v", ...]` or `name = { version = "v" }`."""
    key = (
        rf'(?:"{re.escape(name)}"|{re.escape(name)})'
        if re.fullmatch(r"[A-Za-z0-9_-]+", name)
        else f'"{re.escape(name)}"'
    )
    return re.compile(rf'^(\s*{key}\s*=\s*(?:\[\s*|\{{[^}}]*?\bversion\s*=\s*)?")([^"]*)(")', re.MULTILINE)


def rewrite_tools_section(text: str, updates: dict[str, str]) -> tuple[str, list[str]]:
    """Apply updates inside the [tools] table only; return the new text and the tools whose pin changed."""
    match = re.search(r"^\[tools\]\s*$", text, re.MULTILINE)
    if match is None:
        return text, []
    start = match.end()
    following = re.search(r"^\[", text[start:], re.MULTILINE)
    end = start + following.start() if following else len(text)
    section = text[start:end]
    changed = []
    for name, version in updates.items():
        pattern = tool_line_pattern(name)
        found = pattern.search(section)
        if found and found.group(2) != version:
            section = pattern.sub(lambda m, new=version: f"{m.group(1)}{new}{m.group(3)}", section, count=1)
            changed.append(name)
    return text[:start] + section + text[end:], changed


def set_baseline_ref(path: Path, ref: str) -> None:
    line = f'baseline = "{ref}"'
    if not path.is_file():
        path.write_text(f"{line}\n")
        return
    text = path.read_text()
    if re.search(r"^baseline\s*=", text, re.MULTILINE):
        text = re.sub(r'^baseline\s*=\s*"[^"]*"', line, text, count=1, flags=re.MULTILINE)
    else:
        text = f"{line}\n{text}"
    path.write_text(text)


def sync(baseline_path: Path, repo: Path, ref: str | None) -> list[str]:
    """Move every non-overridden pin to the baseline; return the tools that changed."""
    baseline = load_baseline(baseline_path)
    versions = repo_versions(repo)
    overrides = load_overrides(repo)
    updates = {
        name: baseline[name]
        for name, (version, _) in versions.items()
        if name in baseline and name not in overrides and version != baseline[name]
    }

    changed = []
    mise_path = repo / "mise.toml"
    text, mise_changed = rewrite_tools_section(mise_path.read_text(), {k: v for k, v in updates.items() if k != RUST})
    mise_path.write_text(text)
    changed += mise_changed

    if RUST in updates:
        rust_path = repo / "rust-toolchain.toml"
        if rust_path.is_file():
            rust_text = rust_path.read_text()
            new_text = re.sub(
                r'^(channel\s*=\s*")[^"]*(")', rf"\g<1>{updates[RUST]}\g<2>", rust_text, count=1, flags=re.MULTILINE
            )
            rust_path.write_text(new_text)
        else:
            text, _ = rewrite_tools_section(mise_path.read_text(), {RUST: updates[RUST]})
            mise_path.write_text(text)
        changed.append(RUST)

    missed = sorted(set(updates) - set(changed))
    if missed:
        raise InputError(f"could not rewrite the pin for: {', '.join(missed)}")
    if ref:
        set_baseline_ref(repo / "toolchains.toml", ref)
    return sorted(changed)


def run_check(args: argparse.Namespace) -> int:
    report = check(args.baseline, args.repo)
    for error in report.errors:
        print(f"drift: {error}", file=sys.stderr)
    if not report.checked:
        print("error: no tool in the repository is covered by the baseline; nothing was checked", file=sys.stderr)
        return 2
    print(f"checked {len(report.checked)} tools against the baseline: {', '.join(report.checked)}")
    return 1 if report.errors else 0


def run_sync(args: argparse.Namespace) -> int:
    changed = sync(args.baseline, args.repo, args.ref)
    print(f"updated {len(changed)} pins: {', '.join(changed)}" if changed else "already on the baseline")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name, handler in (("check", run_check), ("sync", run_sync)):
        command = commands.add_parser(name)
        command.add_argument("--baseline", type=Path, required=True, help="path to baseline.toml")
        command.add_argument("--repo", type=Path, default=Path(), help="repository root (default: .)")
        command.set_defaults(handler=handler)
        if name == "sync":
            command.add_argument("--ref", help="baseline tag to record in toolchains.toml")
    args = parser.parse_args(argv)

    try:
        return args.handler(args)
    except (InputError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
