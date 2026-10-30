"""Compare the version files mise does not read with the org baseline, for `toolchains.py check`.

`.nvmrc`, `.node-version`, `.python-version`, `.ruby-version`, `.tool-versions`, nested `rust-toolchain.toml`,
`rust-toolchain` and `package.json` `packageManager: pnpm@X` must state the expected version exactly. Dockerfile
`FROM node|python|golang|ruby:<tag>` lines are compared on major.minor, since the official images publish patch
tags after the release the baseline already follows.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# Directories that hold installed or vendored code, never the repository's own version files.
VENDORED = frozenset({".git", "node_modules", "vendor", "vendored", "third_party", "third-party"})
# Single-version files: the baseline key and the prefix the file may carry before the version.
SINGLE = {
    ".nvmrc": ("node", "v"),
    ".node-version": ("node", "v"),
    ".python-version": ("python", ""),
    ".ruby-version": ("ruby", "ruby-"),
}
VERSION_FILES = frozenset({*SINGLE, ".tool-versions", "rust-toolchain", "rust-toolchain.toml", "package.json"})
DOCKERFILE = re.compile(r"(?:Dockerfile|Containerfile)(?:\.[^/]+)?|[^/]+\.(?:Dockerfile|Containerfile)", re.IGNORECASE)
# Official Docker Hub images and the baseline key their tag versions. ~keep
DOCKER_IMAGES = {"node": "node", "python": "python", "golang": "go", "ruby": "ruby"}
DOCKER_HUB = ("", "library", "docker.io", "docker.io/library", "index.docker.io/library")

# A baseline key to (expected version, how a message names it), or None when the baseline does not cover it.
Wanted = Callable[[str], tuple[str, str] | None]
# A tool name as a file spells it (`nodejs`) to its baseline key, or None.
KeyFor = Callable[[str], str | None]


@dataclass
class Findings:
    errors: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)


def read_text(path: Path) -> str:
    """A file's text, undecodable bytes replaced: a version file in another encoding must not stop the check."""
    return path.read_bytes().decode("utf-8", errors="replace")


def first_value(text: str) -> str:
    """The first line that is neither blank nor a comment, stripped."""
    lines = (line.split("#", 1)[0].strip() for line in text.splitlines())
    return next((line for line in lines if line), "")


def rust_channel(text: str) -> str:
    """The channel of a rust-toolchain(.toml) file: TOML `[toolchain] channel`, or the legacy bare channel line."""
    try:
        toolchain = tomllib.loads(text).get("toolchain")
    except tomllib.TOMLDecodeError:
        return first_value(text)
    channel = toolchain.get("channel") if isinstance(toolchain, dict) else None
    return channel if isinstance(channel, str) else ""


def tool_versions(text: str, key_for: KeyFor) -> list[tuple[str, str]]:
    pins = []
    for line in text.splitlines():
        fields = line.split("#", 1)[0].split()
        key = key_for(fields[0]) if len(fields) >= 2 else None
        if key:
            pins.append((key, fields[1]))
    return pins


def file_pins(path: str, text: str, key_for: KeyFor) -> list[tuple[str, str]]:
    """(baseline key, version) for every version a non-Docker version file states."""
    name = path.rsplit("/", 1)[-1]
    if name in SINGLE:
        key, prefix = SINGLE[name]
        return [(key, first_value(text).removeprefix(prefix))]
    if name == "rust-toolchain" or (name == "rust-toolchain.toml" and "/" in path):
        # The root rust-toolchain.toml is checked with the mise pins. ~keep
        return [("rust", rust_channel(text))]
    if name == "package.json":
        return [("pnpm", m.group(1)) for m in re.finditer(r'"packageManager"\s*:\s*"pnpm@([^"+]*)', text)]
    if name == ".tool-versions":
        return tool_versions(text, key_for)
    return []


def resolve_args(value: str, args: dict[str, str]) -> str | None:
    """value with `$NAME` and `${NAME}` replaced by Dockerfile ARG defaults, or None when one has no default."""
    names = [a or b for a, b in re.findall(r"\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*)", value)]
    if any(name not in args for name in names):
        return None
    return re.sub(r"\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*)", lambda m: args[m.group(1) or m.group(2)], value)


def docker_images(text: str) -> list[tuple[str, str | None, str]]:
    """(baseline key, tag or None when an ARG in it has no default, `image:tag` for messages) for each FROM line
    that uses an official node, python, golang or ruby image. A missing tag is ""."""
    args: dict[str, str] = {}
    images = []
    for line in text.splitlines():
        arg = re.match(r"\s*ARG\s+([A-Za-z_]\w*)=(\S*)", line, re.IGNORECASE)
        if arg:
            args.setdefault(arg.group(1), arg.group(2).strip("\"'"))
            continue
        source = re.match(r"\s*FROM\s+(?:--\S+\s+)*(\S+)", line, re.IGNORECASE)
        if not source:
            continue
        written = source.group(1).split("@", 1)[0]
        resolved = resolve_args(written, args)
        registry, _, shown = (written if resolved is None else resolved).rpartition("/")
        repository, _, tag = shown.lower().partition(":")
        if repository in DOCKER_IMAGES and registry.lower() in DOCKER_HUB:
            images.append((DOCKER_IMAGES[repository], None if resolved is None else tag, shown))
    return images


def major_minor(version: str) -> str:
    return ".".join(version.split(".")[:2])


def check_docker_file(path: str, text: str, wanted: Wanted, found: Findings) -> bool:
    """Report drift in a Dockerfile's official-image tags; True when at least one tag was compared."""
    compared = False
    for key, tag, shown in docker_images(text):
        expected = wanted(key)
        if expected is None:
            continue
        if tag is None:
            found.unchecked.append(f"{path} ({shown})")
            continue
        compared = True
        line, label = major_minor(expected[0]), expected[1]
        number = re.match(r"v?(\d+(?:\.\d+)*)", tag)
        if tag in ("", "latest") or number is None:
            found.errors.append(f"{key}: {path} FROM {shown} has no version tag; pin {line}")
        elif "." not in number.group(1):
            found.errors.append(f"{key}: {path} FROM {shown} names only a major; pin {line}")
        elif major_minor(number.group(1)) != line:
            found.errors.append(f"{key}: {path} FROM {shown} is {major_minor(number.group(1))}, {label}")
    return compared


def check_pins(where: str, pins: list[tuple[str, str]], wanted: Wanted, found: Findings) -> bool:
    """Report every pin that differs from what wanted expects; True when at least one pin was compared."""
    compared = False
    for key, version in pins:
        expected = wanted(key)
        if expected is None:
            continue
        compared = True
        if not version:
            found.errors.append(f"{key}: {where} names no version, {expected[1]}")
        elif version != expected[0]:
            found.errors.append(f"{key}: {where} pins {version}, {expected[1]}")
    return compared


def check(repo: Path, paths: list[str], wanted: Wanted, key_for: KeyFor) -> Findings:
    """Compare the version files among paths (relative to repo) with what wanted expects."""
    found = Findings()
    for path in paths:
        name = path.rsplit("/", 1)[-1]
        if DOCKERFILE.fullmatch(name):
            compared = check_docker_file(path, read_text(repo / path), wanted, found)
        elif name in VERSION_FILES:
            where = f"{path} packageManager" if name == "package.json" else path
            compared = check_pins(where, file_pins(path, read_text(repo / path), key_for), wanted, found)
        else:
            continue
        if compared:
            found.files.append(path)
    return found
