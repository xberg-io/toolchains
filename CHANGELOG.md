# Changelog

## [Unreleased]

### Changed

- gh-actions-updater 0.4.0: `--lint-pins`, the major-pin hooks and the npm channel.

## [1.12.4] - 2026-11-01

### Fixed

- Honor reasoned overrides for tools pinned only in Dockerfiles or version files (#7).
- Continue rejecting unused overrides, missing reasons and mismatched tool versions.

Tool versions and task sets are unchanged from 1.12.3.
