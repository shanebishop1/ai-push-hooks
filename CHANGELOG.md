# Changelog

All notable changes to this project are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Hardened OpenCode execution with isolated configuration, denied tools for read-only work, and allowlisted temporary workspaces for apply steps.
- Added fail-closed path, symlink, file-mode, artifact, Git metadata, and concurrent-change validation around model-assisted edits.
- Restricted prompt files and runtime storage to validated repository-owned locations and private filesystem permissions.
- Updated the default OpenCode model and release workflow dependencies.
- Added CI/package validation, community health files, public package metadata, and post-publication GitHub Releases.

## [0.1.19] - 2026-07-13

### Added

- Added configurable base-branch support for pull-request workflows.
- Added packed npm-package smoke coverage.

### Fixed

- Added Python 3.10 TOML compatibility and fixed npm-local hook command resolution.
- Preserved Git porcelain paths and protected pre-existing dirty allowlisted files during apply steps.
- Clarified module-local artifact references and standardized the repository hook integration.

[Unreleased]: https://github.com/shanebishop1/ai-push-hooks/compare/v0.1.19...HEAD
[0.1.19]: https://github.com/shanebishop1/ai-push-hooks/compare/v0.1.18...v0.1.19
