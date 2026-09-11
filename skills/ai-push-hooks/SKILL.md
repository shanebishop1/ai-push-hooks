---
name: ai-push-hooks
description: AI-powered Git pre-push checks for repository rules, scoped fixes, and explicit quality gates. Use to install, configure, test, or troubleshoot ai-push-hooks with OpenCode, Codex, Claude Code, or custom runners.
---

# ai-push-hooks

`ai-push-hooks` reviews outgoing changes against configured instructions, optionally applies allowlisted fixes, and blocks pushes through explicit checks. It complements linters and tests; AI review is not a guarantee of compliance.

1. Install the CLI, prepare authentication, and integrate or remove hooks using [setup](references/setup.md).
2. Select runners/models, access modes, defaults, and environment overrides using [configuration](references/configuration.md).
3. Build rules checks, assertions, scoped fixes, commands, and callbacks using [workflows](references/workflows.md).
4. Verify installation without pushing, run source tests, and diagnose failures using [testing](references/testing.md).

References are bundled and self-contained; no source checkout is needed except for contributor tests. Run commands from the target repository, not this skill directory. Inspect existing configuration and hooks before editing; preserve unrelated work and never force-replace a hook without approval.

`ask` reports findings; `assert` makes them a gate. `apply` does not auto-commit: review, test, and commit approved edits before retrying. Supply rules such as `AGENTS.md` explicitly as collected context rather than assuming the runner loads them.

Keep fail-closed defaults. Never treat a skipped/fail-open run as validation or bypass checks without explicit approval. Runners, callbacks, and commands are trusted local programs, not an OS sandbox; repository content may reach model providers and local transcripts. Do not push, create PRs, or make billable live probes merely to test setup.
