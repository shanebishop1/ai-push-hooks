# Changelog

All notable changes to this project are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

No changes yet.

## [0.2.0] - 2026-09-06

This is a beta release. Python and npm both use `0.2.0`, the canonical Git tag
is `v0.2.0`, npm uses the `beta` dist-tag, and GitHub marks the release as a
prerelease. PyPI does not provide package channels, so Python users must select
the exact `0.2.0` version rather than a PyPI beta channel.

### Added

- Added a safe repo-local `ai-push-hooks install [--force]` delegate that
  preserves hook arguments, stdin, and exit status, refuses unsafe or shared
  hook targets, and never changes Git configuration.

### Changed

- Hardened OpenCode execution with isolated configuration, denied tools for read-only work, and allowlisted temporary workspaces for apply steps.
- Added fail-closed path, symlink, file-mode, artifact, Git metadata, and concurrent-change validation around model-assisted edits.
- Restricted prompt files and runtime storage to validated repository-owned locations and private filesystem permissions.
- Updated the default OpenCode model and release workflow dependencies.
- Added CI/package validation, community health files, public package metadata, and post-publication GitHub Releases.
- Documented the shortest installed wheel/npm onboarding path, the full-featured
  Lefthook alternative, safe removal, failure/fail-open/skip semantics, and the
  Python requirement for npm installs.
- Recorded a limited synthetic provider preview: OpenCode 1.18.29 with
  `opencode/muse-spark-1.3-contributor-free`, which was listed as free at test
  time; synthetic query/analyze passed at zero reported cost. Free-model
  availability can change. This does not claim all providers/models or live
  apply.

### Compatibility and limitations

- The candidate evidence includes macOS Darwin 24.6.0 arm64 with Python
  3.12.13, Node 24.19.0, npm 10.9.2, Git 2.55.0, Ruff 0.13.3, OpenCode
  1.18.29, `gh` 2.93.0, and `bd` 1.2.2. Python 3.10–3.13 and Node 18+ remain
  declared ranges; Windows has no native beta evidence and is untested/not
  supported for this beta.
- The published `0.1.19` registry artifacts retain their historical provenance;
  this release does not republish, move, or overwrite that version.

## [0.1.19] - 2026-07-13

### Added

- Added configurable base-branch support for pull-request workflows.
- Added packed npm-package smoke coverage.

### Fixed

- Added Python 3.10 TOML compatibility and fixed npm-local hook command resolution.
- Preserved Git porcelain paths and protected pre-existing dirty allowlisted files during apply steps.
- Clarified module-local artifact references and standardized the repository hook integration.

[Unreleased]: https://github.com/shanebishop1/ai-push-hooks/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/shanebishop1/ai-push-hooks/compare/v0.1.19...v0.2.0
[0.1.19]: https://github.com/shanebishop1/ai-push-hooks/compare/v0.1.18...v0.1.19
