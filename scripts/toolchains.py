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
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

RUST = "rust"
# Registry aliases mise accepts for a baseline tool under another name.
ALIASES = {"nodejs": "node", "golang": "go", "tofu": "opentofu"}
QUOTED = r"""(?:"[^"]*"|'[^']*')"""


@dataclass
class Report:
    checked: list[str] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Pin:
    version: str
    options: tuple[tuple[str, str], ...]
    source: str


class InputError(Exception):
    pass


def load_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise InputError(f"{path}: invalid TOML: {exc}") from exc


def read_raw(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def write_raw(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def version_of(value: object, where: str) -> str:
    """Return the default version of a mise tool entry: a string, a list (first entry) or a table."""
    return pin_of(value, where, "").version


def pin_of(value: object, where: str, source: str) -> Pin:
    if isinstance(value, str):
        return Pin(value, (), source)
    if isinstance(value, list) and value:
        return Pin(pin_of(value[0], where, source).version, (), source)
    if isinstance(value, dict) and isinstance(value.get("version"), str):
        options = tuple(sorted((k, str(v)) for k, v in value.items() if k != "version"))
        return Pin(value["version"], options, source)
    raise InputError(f"{where}: cannot read a version from {value!r}")


def load_pins(path: Path, source: str) -> dict[str, Pin]:
    tools = load_toml(path).get("tools", {})
    return {name: pin_of(value, f"{path} [tools].{name}", source) for name, value in tools.items()}


def repo_pins(repo: Path) -> dict[str, Pin]:
    """Map tool name to its pin for everything the repository declares."""
    mise_path = repo / "mise.toml"
    if not mise_path.is_file():
        raise InputError(f"{mise_path}: not found")
    pins = load_pins(mise_path, "mise.toml")
    if RUST in pins:
        raise InputError(f"{mise_path}: declare rust in rust-toolchain.toml, not mise.toml")

    rust_path = repo / "rust-toolchain.toml"
    if rust_path.is_file():
        channel = load_toml(rust_path).get("toolchain", {}).get("channel")
        if not isinstance(channel, str):
            raise InputError(f"{rust_path}: [toolchain].channel missing")
        pins[RUST] = Pin(channel, (), "rust-toolchain.toml")
    return pins


def load_overrides(repo: Path) -> dict[str, dict]:
    path = repo / "toolchains.toml"
    if not path.is_file():
        return {}
    overrides = load_toml(path).get("overrides", {})
    for name, entry in overrides.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("version"), str):
            raise InputError(f"{path}: [overrides.{name}] needs a version string")
    return overrides


def load_baseline(baseline_path: Path) -> dict[str, Pin]:
    baseline = load_pins(baseline_path, "baseline.toml")
    if not baseline:
        raise InputError(f"{baseline_path}: [tools] is empty")
    return baseline


def short_name(name: str) -> str:
    """`aqua:go-task/task` -> `task`, `github:Goldziher/poly` -> `poly`, `nodejs` -> `node`."""
    bare = name.split(":", 1)[-1].rsplit("/", 1)[-1]
    return ALIASES.get(bare, bare)


def baseline_key_for(name: str, baseline: dict[str, Pin]) -> str | None:
    """The baseline key a repository tool stands for when it is spelled differently, else None."""
    if name in baseline:
        return None
    wanted = short_name(name)
    matches = [key for key in baseline if short_name(key) == wanted]
    return matches[0] if len(matches) == 1 else None


def check(baseline_path: Path, repo: Path) -> Report:
    baseline = load_baseline(baseline_path)
    pins = repo_pins(repo)
    overrides = load_overrides(repo)
    report = Report()

    for name, pin in sorted(pins.items()):
        if name not in baseline:
            alias_of = baseline_key_for(name, baseline)
            if alias_of:
                report.errors.append(f"{name}: spelled differently from the baseline; use {alias_of!r}")
            else:
                report.unchecked.append(name)
            continue
        report.checked.append(name)
        expected = baseline[name]
        if expected.options and pin.options != expected.options:
            report.errors.append(
                f"{name}: options {dict(pin.options)} differ from the baseline's {dict(expected.options)}"
            )
        override = overrides.get(name)
        if override is None:
            if pin.version != expected.version:
                report.errors.append(f"{name}: {pin.source} pins {pin.version}, baseline is {expected.version}")
            continue
        if not str(override.get("reason", "")).strip():
            report.errors.append(f"{name}: override in toolchains.toml has no reason")
        elif override["version"] == expected.version:
            report.errors.append(f"{name}: override matches the baseline ({expected.version}); remove it")
        elif override["version"] != pin.version:
            report.errors.append(f"{name}: override says {override['version']}, {pin.source} pins {pin.version}")

    for name in sorted(set(overrides) - set(pins)):
        report.errors.append(f"{name}: override in toolchains.toml for a tool the repository does not pin")
    for name in sorted((set(overrides) & set(pins)) - set(baseline)):
        report.errors.append(f"{name}: override in toolchains.toml for a tool outside the baseline")
    return report


def tool_line_pattern(name: str) -> re.Pattern[str]:
    """Match a [tools] entry's default version: `name = "v"`, `name = ["v", ...]` or `name = { version = "v" }`."""
    quoted = f"\"{re.escape(name)}\"|'{re.escape(name)}'"
    key = f"(?:{quoted}|{re.escape(name)})" if re.fullmatch(r"[A-Za-z0-9_-]+", name) else f"(?:{quoted})"
    prefix = r"(?:\[\s*|\{[^}]*?(?<![\w-])version\s*=\s*)?"
    return re.compile(rf"""^(\s*{key}\s*=\s*{prefix}(["']))(.*?)(\2)""", re.MULTILINE)


def rewrite_tools_section(text: str, updates: dict[str, str]) -> tuple[str, list[str]]:
    """Apply updates inside the [tools] table only; return the new text and the tools whose pin changed."""
    match = re.search(r"^\[tools\][ \t]*(?:#[^\r\n]*)?\r?$", text, re.MULTILINE)
    if match is None:
        return text, []
    start = match.end()
    following = re.search(r"^\s*\[", text[start:], re.MULTILINE)
    end = start + following.start() if following else len(text)
    section = text[start:end]
    changed = []
    for name, version in updates.items():
        pattern = tool_line_pattern(name)
        if pattern.search(section):
            section = pattern.sub(lambda m, new=version: f"{m.group(1)}{new}{m.group(4)}", section, count=1)
            changed.append(name)
    return text[:start] + section + text[end:], changed


def with_baseline_ref(text: str | None, ref: str) -> str:
    line = f'baseline = "{ref}"'
    if text is None:
        return f"{line}\n"
    if not re.search(r"^baseline\s*=", text, re.MULTILINE):
        return f"{line}\n{text}"
    new_text, count = re.subn(rf"^baseline\s*=\s*{QUOTED}", line, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise InputError('toolchains.toml: cannot rewrite the baseline line; expected baseline = "<tag>"')
    return new_text


def sync(baseline_path: Path, repo: Path, ref: str | None) -> list[str]:
    """Move every non-overridden pin to the baseline; return the tools that changed. Writes nothing on error."""
    baseline = load_baseline(baseline_path)
    pins = repo_pins(repo)
    overrides = load_overrides(repo)
    covered = {name: pin for name, pin in pins.items() if name in baseline and name not in overrides}
    mismatched = sorted(
        name for name, pin in covered.items() if baseline[name].options and pin.options != baseline[name].options
    )
    if mismatched:
        raise InputError(f"copy the baseline's table options by hand for: {', '.join(mismatched)}")
    updates = {name: baseline[name].version for name, pin in covered.items() if pin.version != baseline[name].version}

    writes: dict[Path, str] = {}
    changed: list[str] = []
    mise_path = repo / "mise.toml"
    text, mise_changed = rewrite_tools_section(read_raw(mise_path), {k: v for k, v in updates.items() if k != RUST})
    writes[mise_path] = text
    changed += mise_changed

    if RUST in updates:
        rust_path = repo / "rust-toolchain.toml"
        rust_text, count = re.subn(
            r"""^(\s*channel\s*=\s*)(["']).*?(\2)""",
            lambda m: f"{m.group(1)}{m.group(2)}{updates[RUST]}{m.group(3)}",
            read_raw(rust_path),
            count=1,
            flags=re.MULTILINE,
        )
        if count == 1:
            writes[rust_path] = rust_text
            changed.append(RUST)

    missed = sorted(set(updates) - set(changed))
    if missed:
        raise InputError(
            f"could not rewrite the pin for: {', '.join(missed)} "
            '(sync edits `name = "v"`, `name = ["v", ...]` and inline `{ version = "v" }` entries under [tools])'
        )
    toolchains_path = repo / "toolchains.toml"
    if ref:
        writes[toolchains_path] = with_baseline_ref(
            read_raw(toolchains_path) if toolchains_path.is_file() else None, ref
        )
    for path, content in writes.items():
        write_raw(path, content)
    return sorted(changed)


def run_check(args: argparse.Namespace) -> int:
    report = check(args.baseline, args.repo)
    for error in report.errors:
        print(f"drift: {error}", file=sys.stderr)
    if not report.checked:
        print("error: no tool in the repository is covered by the baseline; nothing was checked", file=sys.stderr)
        return 2
    print(f"checked {len(report.checked)} tools against the baseline: {', '.join(report.checked)}")
    if report.unchecked:
        print(f"repository-owned, not in the baseline: {', '.join(report.unchecked)}")
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
