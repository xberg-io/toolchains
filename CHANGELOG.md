# Changelog

## [Unreleased]

## [1.16.0] - 2026-11-06

### Added

- Go CI tools in the baseline: `golangci-lint` 2.15.0, `go:golang.org/x/vuln/cmd/govulncheck` 1.9.0 and
  `go:github.com/google/go-licenses/v2` 2.0.1, as the org Go rule requires them in CI.

## [1.15.1] - 2026-11-06

### Changed

- alef 0.127.1: generated Package.swift defaults to macOS 15, and alef's format gates work with poly 0.33.
- poly 0.33.1 (Renovate). This repository follows its own baseline again: its `mise.toml` pinned poly 0.32.5
  while the Renovate lock update recorded 0.33.1, which failed CI.

### Fixed

- The `lint-format` task set's `format:check` runs `poly fmt .`: poly 0.33 removed `--check` (the dry run is the
  default and exits 1 when a file would change), so the shared task failed in every repository on the 0.33 baseline.

## [1.15.0] - 2026-11-06

### Changed

- `[platforms] min_macos_version = "15.0"` (was `26.0`): macOS 15 is the minimum for every macOS artifact, the oldest
  macOS Apple still supports. It moves when Apple drops a release.

## [1.14.3] - 2026-11-05

### Fixed

- poly is pinned as `0.32.5` again: the Renovate baseline update in 1.14.2 wrote `v0.32.5`, unlike every other pin.
  Renovate now strips a leading `v` from GitHub release tags.

### Changed

- cargo-zigbuild 0.24.1, OpenTofu 1.13.8, aws-cli 2.39.4 (Renovate, shipped in 1.14.2).

## [1.14.2] - 2026-11-05

### Changed

- alef 0.124.2: generated Dart e2e tests compare `u64` streaming-chunk fields as `BigInt`.

## [1.14.1] - 2026-11-05

### Changed

- alef 0.124.1: generated Dart bindings no longer cast primitives to their own type, which failed `clippy -D warnings`.

## [1.14.0] - 2026-11-04

### Added

- `[platforms] min_macos_version = "26.0"` in `baseline.toml`: one macOS minimum for every repository, in alef's
  `min_macos_version` name and form. `check` and `sync` reject an unknown `[platforms]` key or a value that is not
  `<major>.<minor>` (#11).

### Changed

- alef 0.124.0 (generated Python packages now require Python 3.11, from alef 0.117.0; private-field accessors keep
  their values in every binding), uv 0.13.7.
- poly 0.32.5: git hooks resolve the mise-pinned poly without shell activation, and Windows hook source checks keep
  their quoting.

## [1.13.2] - 2026-11-01

### Changed

- alef 0.115.13: Zig bool C ABI, extendr primitive bridge parameters, AGP 9 `copyHostJni` ordering.

## [1.13.1] - 2026-11-01

### Added

- `file_overrides` in `toolchains.toml` exempts a version file, such as a nested MSRV `rust-toolchain.toml`, from
  `sync` and `check`, with a reason.

### Changed

- poly 0.32.2: Windows hook commands keep their argument quoting.

### Fixed

- `check` reports, and `sync` moves, a baseline tool's version pinned in a mise.toml task's `tools`.
- `sync` recomputes a `packageManager` `+sha512.<hex>` suffix for the new pnpm release instead of dropping it.

## [1.13.0] - 2026-11-01

### Added

- gh-actions-updater 0.4.0: `--lint-pins`, the major-pin hooks and the npm channel.

### Changed

- node 26.13.0, pnpm 12.16.1, python 3.15.2, ruby 4.0.9.
- poly 0.32.1, alef 0.115.12, wasm-pack 0.15.1, kubectl 1.36.6, aws-cli 2.39.3.
- Renovate no longer updates GitHub Actions refs; gh-actions-updater owns them (org ADR-0008, ADR-0019).
- Renovate moves task-set node pins in the same pull request as the baseline.

## [1.12.4] - 2026-11-01

### Fixed

- Honor reasoned overrides for tools pinned only in Dockerfiles or version files (#7).
- Continue rejecting unused overrides, missing reasons and mismatched tool versions.

Tool versions and task sets are unchanged from 1.12.3.
