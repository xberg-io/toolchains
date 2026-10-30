#!/usr/bin/env python3
"""Keep a repository's mise toolchain on the org baseline.

`check` reads <repo>/mise.toml, <repo>/rust-toolchain.toml and <repo>/toolchains.toml. Every tool the repository
declares that the baseline also lists must use the baseline version, unless toolchains.toml records an
override with a reason. Pinning ai-rulez, which always runs at its newest release, is drift. It also fails
when the repository root holds a go-task Taskfile or `.task/` directory, since mise runs the org's tasks.
Exits 0 when consistent, 1 on drift, 2 on unusable input.

`sync` rewrites those pins to the baseline versions in place, skipping overridden tools, and records the
baseline ref in toolchains.toml. It edits matching lines only, so comments and layout survive.

Task sets: toolchains.toml `task_sets` names sets under task-sets/. `sync` writes their tasks to
mise-tasks/shared.toml, copies their file tasks to mise-tasks/.shared/ and adds both to mise.toml's
[task_config] includes. `check` fails when those generated files drift, when a selected set is unknown, or
when the repository defines a task a set provides without a `task_overrides` entry giving the reason.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

RUST = "rust"
# Registry aliases mise accepts for a baseline tool under another name.
ALIASES = {"nodejs": "node", "golang": "go", "tofu": "opentofu"}
# Tools no repository may pin, in any backend spelling, with the replacement.
UNPINNED = {"ai-rulez": "remove it from mise.toml and run `npx -y ai-rulez@latest`"}
QUOTED = r"""(?:"[^"]*"|'[^']*')"""
# Every root file name go-task v3 reads, in any letter case (macOS and Windows match case-insensitively).
TASKFILE = re.compile(r"taskfile(?:\.dist)?\.ya?ml", re.IGNORECASE)
TASK_DIR = ".task"

SETS_DIR = Path(__file__).resolve().parent.parent / "task-sets"
SET_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
SHARED_TOML = "mise-tasks/shared.toml"
# Hidden, because mise registers every executable under an included directory as a task of its own. ~keep
SHARED_DIR = "mise-tasks/.shared"
MANIFEST = f"{SHARED_DIR}/.manifest"
REQUIRED_INCLUDES = ("mise-tasks", SHARED_TOML)
# Directories mise scans for file tasks when [task_config] includes is absent; setting includes replaces them.
MISE_DEFAULT_TASK_DIRS = ("mise-tasks", ".mise-tasks", ".mise/tasks", "mise/tasks", ".config/mise/tasks")
EXECUTABLE = 0o755
# Windows has no exec bit: never compare or report it there. ~keep
POSIX_MODES = os.name != "nt"
# Editor and patch leftovers in a set directory that sync must not ship.
BACKUP_SUFFIXES = ("~", ".bak", ".orig", ".rej", ".swp")


@dataclass
class Report:
    checked: list[str] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    tasks: list[str] = field(default_factory=list)


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
    # A symlinked mise.toml keeps its link: write the file it points to. ~keep
    write_atomic(path.resolve() if path.is_symlink() else path, text.encode("utf-8"))


def parse_toml(text: str, where: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise InputError(f"{where}: invalid TOML: {exc}") from exc


def write_atomic(path: Path, content: bytes, mode: int | None = None) -> None:
    """Replace path in one rename, so an interrupted run never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        Path(tmp).chmod(mode)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


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
    """`aqua:mikefarah/yq` -> `yq`, `github:Goldziher/poly` -> `poly`, `nodejs` -> `node`."""
    bare = name.split(":", 1)[-1].rsplit("/", 1)[-1]
    return ALIASES.get(bare, bare)


def baseline_key_for(name: str, baseline: dict[str, Pin]) -> str | None:
    """The baseline key a repository tool stands for when it is spelled differently, else None."""
    if name in baseline:
        return None
    wanted = short_name(name)
    matches = [key for key in baseline if short_name(key) == wanted]
    return matches[0] if len(matches) == 1 else None


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def root_entries(repo: Path) -> list[str]:
    """Names at the repository root, a directory as `name/`: the tracked ones in a git checkout, else on disk."""
    try:
        is_checkout = Path(git(repo, "rev-parse", "--show-toplevel").strip()).resolve() == repo.resolve()
    except (OSError, subprocess.CalledProcessError):
        is_checkout = False
    if not is_checkout:
        return sorted(f"{entry.name}/" if entry.is_dir() else entry.name for entry in repo.iterdir())
    names = set()
    for path in git(repo, "ls-files", "-z").split("\0"):
        if path:
            head, slash, _ = path.partition("/")
            names.add(f"{head}{slash}")
    return sorted(names)


def go_task_leftovers(repo: Path) -> list[str]:
    """Root Taskfiles and `.task/` the repository still carries."""
    return [
        name
        for name in root_entries(repo)
        if name == f"{TASK_DIR}/" or (not name.endswith("/") and TASKFILE.fullmatch(name))
    ]


@dataclass(frozen=True)
class TaskSet:
    name: str
    chunks: tuple[tuple[str, str], ...]
    tasks: dict[str, dict]
    files: dict[str, bytes]


@dataclass(frozen=True)
class TaskConfig:
    sets: tuple[str, ...]
    overrides: dict[str, str]
    ref: str | None


def load_task_config(repo: Path) -> TaskConfig:
    path = repo / "toolchains.toml"
    data = load_toml(path) if path.is_file() else {}
    sets = data.get("task_sets", [])
    if not isinstance(sets, list) or not all(isinstance(name, str) for name in sets):
        raise InputError(f"{path}: task_sets must be a list of set names")
    if len(set(sets)) != len(sets):
        raise InputError(f"{path}: task_sets lists a set twice")
    overrides = data.get("task_overrides", {})
    if not isinstance(overrides, dict) or not all(isinstance(reason, str) for reason in overrides.values()):
        raise InputError(f'{path}: task_overrides must map a task name to a reason: {{ "<task>" = "<reason>" }}')
    ref = data.get("baseline")
    return TaskConfig(tuple(sets), overrides, ref if isinstance(ref, str) else None)


def split_tasks(text: str, where: str) -> tuple[tuple[str, str], ...]:
    """Split a mise task file into (task name, text) chunks; comments directly above a header go with it."""
    chunks: list[tuple[str, list[str]]] = [("", [])]
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not line.startswith("[") or not parses("".join(lines[:index])):
            # Not a header: a line of a multi-line string or array that starts with "[". ~keep
            chunks[-1][1].append(line)
            continue
        names = list(parse_toml(line, f"{where}: table header {line.strip()!r}"))
        previous = chunks[-1][1]
        lead = len(previous)
        while lead and previous[lead - 1].lstrip().startswith("#"):
            lead -= 1
        chunks.append((names[0], [*previous[lead:], line]))
        del previous[lead:]
    return tuple((name, "".join(lines)) for name, lines in chunks if name or "".join(lines).strip())


def parses(text: str) -> bool:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    return True


def shippable(relative: Path) -> bool:
    """False for hidden files and editor or patch leftovers in a set directory."""
    hidden = any(part.startswith(".") for part in relative.parts)
    return not hidden and not relative.name.endswith(BACKUP_SUFFIXES)


def aliases_of(task: object) -> list[str]:
    alias = task.get("alias", []) if isinstance(task, dict) else []
    return [alias] if isinstance(alias, str) else [name for name in alias if isinstance(name, str)]


def load_task_set(sets_dir: Path, name: str) -> TaskSet | None:
    """Read task-sets/<name>.toml and its file tasks under task-sets/<name>/; None when no such set exists."""
    path = sets_dir / f"{name}.toml"
    if not SET_NAME.fullmatch(name) or not path.is_file():
        return None
    text = read_raw(path)
    tasks = load_toml(path)
    chunks = split_tasks(text, str(path))
    if sorted({task for task, _ in chunks if task}) != sorted(tasks):
        raise InputError(f"{path}: every task must be one top-level table")
    files = {}
    root = sets_dir / name
    if root.is_dir():
        for source in sorted(p for p in root.rglob("*") if p.is_file() and shippable(p.relative_to(root))):
            files[f"{SHARED_DIR}/{name}/{source.relative_to(root).as_posix()}"] = source.read_bytes()
    for task, entry in tasks.items():
        target = entry.get("file") if isinstance(entry, dict) else None
        if isinstance(target, str) and target not in files:
            raise InputError(f"{path}: {task} runs {target}, which the set does not ship under {root}")
    return TaskSet(name, chunks, tasks, files)


def resolve_sets(sets_dir: Path, names: tuple[str, ...]) -> tuple[list[TaskSet], list[str]]:
    """The selected sets in order, and the names that match no set. Two sets may not define one task."""
    found, unknown, owner = [], [], {}
    for name in names:
        task_set = load_task_set(sets_dir, name)
        if task_set is None:
            unknown.append(name)
            continue
        for task in task_set.tasks:
            for provided in [task, *aliases_of(task_set.tasks[task])]:
                if provided in owner:
                    raise InputError(f"task sets {owner[provided]} and {name} both define {provided}")
                owner[provided] = name
        found.append(task_set)
    return found, unknown


def provided_tasks(sets: list[TaskSet], overrides: dict[str, str]) -> dict[str, tuple[str, str]]:
    """Map every task name and alias the shared file defines to (set, task), leaving out overridden tasks."""
    provided = {}
    for task_set in sets:
        for task, entry in task_set.tasks.items():
            if task not in overrides:
                for name in [task, *aliases_of(entry)]:
                    provided[name] = (task_set.name, task)
    return provided


def render_shared(sets: list[TaskSet], overrides: dict[str, str], ref: str | None) -> str:
    names = ", ".join(task_set.name for task_set in sets)
    parts = [
        f"# Generated by scripts/toolchains.py sync from xberg-io/toolchains {ref or '(no baseline tag)'}.\n"
        "# Do not edit: change the set in xberg-io/toolchains, or list the task under task_overrides in\n"
        "# toolchains.toml and define it in this repository.\n"
        f"# Task sets: {names}\n"
    ]
    for task_set in sets:
        body = "".join(text for task, text in task_set.chunks if task not in overrides).strip("\n")
        omitted = sorted(task for task in task_set.tasks if task in overrides)
        note = f"\n# Omitted by task_overrides: {', '.join(omitted)}" if omitted else ""
        parts.append(f"\n# Task set: {task_set.name}{note}\n{body}\n")
    return "".join(parts)


def expected_shared_files(sets: list[TaskSet], config: TaskConfig, ref: str | None) -> dict[str, tuple[bytes, int]]:
    """Every file sync owns for the selected sets, keyed by repository-relative path, with its mode."""
    if not sets:
        return {}
    files = {SHARED_TOML: (render_shared(sets, config.overrides, ref).encode("utf-8"), 0o644)}
    for task_set in sets:
        files.update({path: (content, EXECUTABLE) for path, content in task_set.files.items()})
    listing = "".join(f"{path}\n" for path in sorted(files))
    header = "# Files scripts/toolchains.py sync wrote for task sets; it deletes them when no set ships them.\n"
    files[MANIFEST] = ((header + listing).encode("utf-8"), 0o644)
    return files


def owned_target(repo: Path, relative: str) -> Path:
    """repo/relative for a path sync may write or delete; InputError for any other path or one through a link."""
    parts = relative.split("/")
    inside = relative in (SHARED_TOML, MANIFEST) or relative.startswith(f"{SHARED_DIR}/")
    if not inside or "\\" in relative or any(part in ("", ".", "..") for part in parts):
        raise InputError(f"{relative}: outside the paths task sets own ({SHARED_TOML}, {SHARED_DIR}/)")
    path = repo
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise InputError(f"{relative}: {path.relative_to(repo).as_posix()} is a symlink; sync does not follow it")
    root = (repo / "mise-tasks").resolve()
    if not path.resolve().is_relative_to(root):
        raise InputError(f"{relative}: resolves outside {root}")
    return path


def manifest_paths(repo: Path) -> list[str]:
    """Paths the previous sync recorded, plus the manifest itself. InputError for an entry sync could not own."""
    path = owned_target(repo, MANIFEST)
    if not path.is_file():
        return []
    entries = [line.strip() for line in read_raw(path).splitlines()]
    owned = [entry for entry in entries if entry and not entry.startswith("#")]
    for entry in owned:
        owned_target(repo, entry)
    return [*owned, MANIFEST]


def mise_includes(data: dict) -> list[str] | None:
    """[task_config] includes, or None when unset. InputError when it is not a list of strings."""
    config = data.get("task_config", {})
    if not isinstance(config, dict) or "includes" not in config:
        return None
    includes = config["includes"]
    if not isinstance(includes, list) or not all(isinstance(entry, str) for entry in includes):
        raise InputError("mise.toml: [task_config] includes must be a list of paths")
    return includes


def normalized(includes: list[str] | None) -> list[str]:
    """Include paths as mise resolves them, so "./mise-tasks" and "mise-tasks/" both mean "mise-tasks"."""
    return [posixpath.normpath(entry) for entry in includes or []]


def task_config_span(text: str) -> tuple[re.Match[str] | None, int]:
    """The [task_config] header match and the offset where its section ends."""
    # The lookahead keeps a CRLF line's \r after the match, so text inserted at its end lands before it. ~keep
    header = re.search(r"^\[task_config\][ \t]*(?:#[^\r\n]*)?(?=\r?$)", text, re.MULTILINE)
    if not header:
        return None, len(text)
    following = re.search(r"^\s*\[", text[header.end() :], re.MULTILINE)
    return header, header.end() + following.start() if following else len(text)


def with_shared_includes(text: str, repo: Path) -> str:
    """mise.toml text whose [task_config] includes lists mise-tasks and the shared task file."""
    data = parse_toml(text, "mise.toml")
    includes = mise_includes(data)
    if includes is None:
        # Setting includes replaces mise's default scan; keep every default directory the repository uses. ~keep
        wanted = [name for name in MISE_DEFAULT_TASK_DIRS[1:] if (repo / name).is_dir()]
        missing = ["mise-tasks", *wanted, SHARED_TOML]
    else:
        missing = [name for name in REQUIRED_INCLUDES if name not in normalized(includes)]
    if not missing:
        return text
    newline = "\r\n" if "\r\n" in text else "\n"
    entries = ", ".join(f'"{name}"' for name in missing)
    header, end = task_config_span(text)
    if "task_config" not in data:
        separator = "" if not text or text.endswith(newline) else newline
        new_text = f"{text}{separator}{newline}[task_config]{newline}includes = [{entries}]{newline}"
    elif header and includes is None:
        new_text = f"{text[: header.end()]}{newline}includes = [{entries}]{text[header.end() :]}"
    else:
        array = None
        if header:
            array = re.compile(r"^[ \t]*includes[ \t]*=[ \t]*\[", re.MULTILINE).search(text, header.end(), end)
        if array is None:
            raise InputError(f"mise.toml: add {entries} to [task_config] includes by hand")
        rest = text[array.end() :]
        if rest.startswith(("\n", "\r\n")):
            indent = re.match(r"\r?\n([ \t]*)", rest).group(1) or "  "
            insert = "".join(f'{newline}{indent}"{name}",' for name in missing)
        else:
            insert = entries if rest.lstrip().startswith("]") else f"{entries}, "
        new_text = text[: array.end()] + insert + rest
    if not set(REQUIRED_INCLUDES) <= set(normalized(mise_includes(parse_toml(new_text, "mise.toml")))):
        raise InputError(f"mise.toml: add {entries} to [task_config] includes by hand")
    return new_text


def without_shared_include(text: str) -> str:
    """mise.toml text with the shared task file dropped from [task_config] includes, once no set is selected."""
    data = parse_toml(text, "mise.toml")
    includes = mise_includes(data) or []
    raw = [name for name in includes if posixpath.normpath(name) == SHARED_TOML]
    if not raw:
        return text
    header, end = task_config_span(text)
    start = header.end() if header else end
    section = text[start:end]
    entry = re.escape(f'"{raw[0]}"')
    # An entry on its own line, else one inside a single-line array with its separating comma. ~keep
    new_section, count = re.subn(rf"^[ \t]*{entry}[ \t]*,?[ \t]*\r?\n", "", section, count=1, flags=re.MULTILINE)
    if not count:
        new_section, count = re.subn(rf"{entry}[ \t]*,[ \t]*|,?[ \t]*{entry}", "", section, count=1)
    new_text = text[:start] + new_section + text[end:]
    kept = [name for name in includes if posixpath.normpath(name) != SHARED_TOML]
    expected = {**data["task_config"], "includes": kept}
    if not count or parse_toml(new_text, "mise.toml").get("task_config") != expected:
        raise InputError(f'mise.toml: remove "{SHARED_TOML}" from [task_config] includes by hand')
    return new_text


def task_file_name(relative: Path) -> str:
    parts = [*relative.parent.parts, relative.stem]
    if parts[-1] == "_default" and len(parts) > 1:
        parts.pop()
    return ":".join(parts)


def table_tasks(tables: dict) -> list[str]:
    return [alias for name, task in tables.items() for alias in [name, *aliases_of(task)]]


def directory_tasks(repo: Path, directory: str) -> dict[str, str]:
    """Tasks mise registers from an included directory, by name: the tables of every *.toml under it and every
    other executable file, skipping hidden paths and the shared task file."""
    found: dict[str, str] = {}
    root = repo / directory
    shared = (repo / SHARED_TOML).resolve()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        source = f"{directory}/{relative.as_posix()}"
        if any(part.startswith(".") for part in relative.parts) or not path.is_file() or path.resolve() == shared:
            continue
        if path.suffix == ".toml":
            for name in table_tasks(load_toml(path)):
                found.setdefault(name, source)
        elif os.access(path, os.X_OK):
            found.setdefault(task_file_name(relative), source)
    return found


def local_tasks(repo: Path) -> dict[str, str]:
    """Map each task name and alias the repository defines itself to where it is defined, reading what mise
    reads: mise.toml [tasks] and [task_config] includes, or mise's default task directories without it."""
    mise_path = repo / "mise.toml"
    data = load_toml(mise_path) if mise_path.is_file() else {}
    found = dict.fromkeys(table_tasks(data.get("tasks", {})), "mise.toml")
    includes = mise_includes(data)
    for entry in dict.fromkeys(MISE_DEFAULT_TASK_DIRS if includes is None else normalized(includes)):
        path = repo / entry
        if path.is_dir():
            for name, source in directory_tasks(repo, entry).items():
                found.setdefault(name, source)
        elif path.is_file() and path.resolve() != (repo / SHARED_TOML).resolve():
            for name in table_tasks(load_toml(path)):
                found.setdefault(name, entry)
    return found


def check_shared_files(repo: Path, expected: dict[str, tuple[bytes, int]], report: Report) -> None:
    on_disk = set(manifest_paths(repo))
    shared_dir = repo / SHARED_DIR
    # Only a repository that uses task sets (or did) owns the whole of mise-tasks/.shared/. ~keep
    if shared_dir.is_dir() and (expected or on_disk):
        on_disk |= {p.relative_to(repo).as_posix() for p in shared_dir.rglob("*") if p.is_file()}
    for path in sorted(on_disk - set(expected)):
        if (repo / path).exists():
            report.errors.append(
                f"{path}: not written by sync for the selected task sets; sync never deletes it, so delete it"
            )
    for path, (content, mode) in sorted(expected.items()):
        target = repo / path
        if not target.is_file():
            report.errors.append(f"{path}: missing; run sync")
        elif target.read_bytes() != content:
            report.errors.append(f"{path}: differs from the task sets; run sync (edit the set in xberg-io/toolchains)")
        elif POSIX_MODES and is_executable(target) != (mode == EXECUTABLE):
            state = "not executable" if mode == EXECUTABLE else "executable"
            report.errors.append(f"{path}: {state}; run sync")


def is_executable(path: Path) -> bool:
    return bool(path.stat().st_mode & 0o100)


def check_overrides(sets: list[TaskSet], config: TaskConfig, local: dict[str, str], report: Report) -> None:
    provided = provided_tasks(sets, {})
    for name, reason in sorted(config.overrides.items()):
        if name not in provided:
            report.errors.append(f"{name}: task_overrides entry for a task no selected set provides")
        elif provided[name][1] != name:
            report.errors.append(f"{name}: task_overrides names an alias; override {provided[name][1]!r} instead")
        elif name not in local:
            report.errors.append(
                f"{name}: task_overrides entry for a task this repository does not define; delete the entry to "
                "take the shared task, or define the task"
            )
        if name in provided and not reason.strip():
            report.errors.append(f"{name}: task_overrides entry has no reason")


def check_task_names(repo: Path, sets: list[TaskSet], config: TaskConfig, report: Report) -> None:
    if not sets and not config.overrides:
        return
    local = local_tasks(repo)
    check_overrides(sets, config, local, report)
    if not sets:
        return
    mise_path = repo / "mise.toml"
    includes = mise_includes(load_toml(mise_path)) if mise_path.is_file() else None
    for name in REQUIRED_INCLUDES:
        if name not in normalized(includes):
            report.errors.append(f'mise.toml: [task_config] includes lacks "{name}"; run sync')
    active = provided_tasks(sets, config.overrides)
    for name, source in sorted(local.items()):
        if name in active:
            set_name, task = active[name]
            alias = f" (alias of {task})" if task != name else ""
            report.errors.append(
                f"{name}: {source} defines a task task set {set_name} provides{alias}; delete it, or keep it with "
                f'task_overrides = {{ "{task}" = "<reason>" }} in toolchains.toml'
            )
    report.tasks = sorted(name for name, (_, task) in active.items() if name == task)


def check_task_sets(repo: Path, sets_dir: Path, report: Report) -> None:
    config = load_task_config(repo)
    sets, unknown = resolve_sets(sets_dir, config.sets)
    report.errors += [f"task set {name!r}: no such set in {sets_dir}" for name in unknown]
    check_shared_files(repo, expected_shared_files(sets, config, config.ref), report)
    check_task_names(repo, sets, config, report)
    check_task_tool_locks(repo, sets, config, report)


def check_task_tool_locks(repo: Path, sets: list[TaskSet], config: TaskConfig, report: Report) -> None:
    """A committed mise.lock must hold every tool version a shipped task pins, or `--locked` CI cannot install it."""
    path = repo / "mise.lock"
    if not path.is_file():
        return
    locked = load_toml(path).get("tools", {})
    for task_set in sets:
        for task, entry in task_set.tasks.items():
            tools = entry.get("tools", {}) if task not in config.overrides else {}
            for tool, version in tools.items():
                entries = locked.get(tool, [])
                entries = [entries] if isinstance(entries, dict) else entries
                if version not in {item.get("version") for item in entries if isinstance(item, dict)}:
                    report.errors.append(
                        f"{task}: mise.lock lacks {tool} {version}, which the task's tools pin; run mise lock"
                    )


def plan_task_sets(repo: Path, sets_dir: Path, ref: str | None, mise_text: str) -> tuple[dict, list[str], str]:
    """Files to write, files to delete and the new mise.toml text for the selected sets. Raises on bad input."""
    config = load_task_config(repo)
    sets, unknown = resolve_sets(sets_dir, config.sets)
    if unknown:
        raise InputError(f"no such task set in {sets_dir}: {', '.join(unknown)}")
    expected = expected_shared_files(sets, config, ref or config.ref)
    previous = manifest_paths(repo)
    for path, (content, _) in expected.items():
        target = owned_target(repo, path)
        if path not in previous and target.exists() and (not target.is_file() or target.read_bytes() != content):
            raise InputError(f"{path}: exists and sync did not write it; move it away, then run sync")
    stale = []
    for path in previous:
        target = owned_target(repo, path)
        if path not in expected and target.is_file():
            stale.append(path)
    mise_text = with_shared_includes(mise_text, repo) if sets else without_shared_include(mise_text)
    return expected, stale, mise_text


def unpinned_key(name: str) -> str:
    """`go:github.com/Goldziher/ai-rulez/v5`, `npm:AI_Rulez`, `github:Goldziher/ai-rulez[bin=x]` -> `ai-rulez`."""
    bare = re.sub(r"\[[^\]]*\]$", "", name).lower().replace("_", "-")
    return short_name(re.sub(r"/v\d+$", "", bare))


def without_unpinned(pins: dict[str, Pin], report: Report) -> dict[str, Pin]:
    """Report every pin of a tool that must run unpinned, whatever backend spells it; return the other pins."""
    for name in sorted(pins):
        if unpinned_key(name) in UNPINNED:
            report.errors.append(f"{name}: {unpinned_key(name)} is never pinned; {UNPINNED[unpinned_key(name)]}")
    return {name: pin for name, pin in pins.items() if unpinned_key(name) not in UNPINNED}


def check(baseline_path: Path, repo: Path, sets_dir: Path = SETS_DIR) -> Report:
    baseline = load_baseline(baseline_path)
    pins = repo_pins(repo)
    overrides = load_overrides(repo)
    report = Report()

    for name, pin in sorted(without_unpinned(pins, report).items()):
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
    report.errors += [
        f"{name}: go-task leftover; tasks run with mise now, so port it to mise.toml or mise-tasks/ and delete it"
        for name in go_task_leftovers(repo)
    ]
    check_task_sets(repo, sets_dir, report)
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


def sync(baseline_path: Path, repo: Path, ref: str | None, sets_dir: Path = SETS_DIR) -> tuple[list[str], list[str]]:
    """Move every non-overridden pin to the baseline and write the selected task sets.

    Returns the tools whose pin changed and the shared task files written or deleted. Writes nothing on error.
    """
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
    shared, stale, text = plan_task_sets(repo, sets_dir, ref, text)
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
    # Shared files first: an interrupted sync then leaves mise.toml unchanged rather than including a missing file.
    touched = write_shared(repo, shared, stale)
    for path, content in writes.items():
        if not path.is_file() or read_raw(path) != content:
            write_raw(path, content)
    return sorted(changed), touched


def write_shared(repo: Path, shared: dict[str, tuple[bytes, int]], stale: list[str]) -> list[str]:
    """Write the shared task files that changed and delete stale ones; return what was touched."""
    touched = []
    # The manifest goes last, so an interrupted sync still lists every file it may have to delete. ~keep
    for relative, (content, mode) in sorted(shared.items(), key=lambda item: item[0] == MANIFEST):
        if relative == MANIFEST:
            delete_stale(repo, stale, touched)
        target = owned_target(repo, relative)
        # Compare only the exec bit: group bits follow the umask and must not make every sync rewrite. ~keep
        exists = target.is_file()
        exec_differs = POSIX_MODES and exists and is_executable(target) != (mode == EXECUTABLE)
        if not exists or target.read_bytes() != content or exec_differs:
            write_atomic(target, content, mode)
            touched.append(relative)
    if MANIFEST not in shared:
        delete_stale(repo, stale, touched)
    return touched


def delete_stale(repo: Path, stale: list[str], touched: list[str]) -> None:
    for relative in sorted(stale, key=lambda path: path == MANIFEST):
        target = owned_target(repo, relative)
        target.unlink()
        touched.append(f"{relative} (deleted)")
        parent = target.parent
        while parent != repo / "mise-tasks" and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent


def run_check(args: argparse.Namespace) -> int:
    report = check(args.baseline, args.repo, args.sets_dir)
    for error in report.errors:
        print(f"drift: {error}", file=sys.stderr)
    if not report.checked:
        print("error: no tool in the repository is covered by the baseline; nothing was checked", file=sys.stderr)
        return 2
    print(f"checked {len(report.checked)} tools against the baseline: {', '.join(report.checked)}")
    if report.unchecked:
        print(f"repository-owned, not in the baseline: {', '.join(report.unchecked)}")
    if report.tasks:
        print(f"checked {len(report.tasks)} shared tasks: {', '.join(report.tasks)}")
    return 1 if report.errors else 0


def run_sync(args: argparse.Namespace) -> int:
    changed, touched = sync(args.baseline, args.repo, args.ref, args.sets_dir)
    print(f"updated {len(changed)} pins: {', '.join(changed)}" if changed else "already on the baseline")
    if touched:
        print(f"updated {len(touched)} shared task files: {', '.join(touched)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name, handler in (("check", run_check), ("sync", run_sync)):
        command = commands.add_parser(name)
        command.add_argument("--baseline", type=Path, required=True, help="path to baseline.toml")
        command.add_argument("--repo", type=Path, default=Path(), help="repository root (default: .)")
        command.add_argument(
            "--sets-dir", type=Path, default=SETS_DIR, help="task set directory (default: this checkout's task-sets/)"
        )
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
