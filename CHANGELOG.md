# Changelog

## [Unreleased]

### Fixed

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
