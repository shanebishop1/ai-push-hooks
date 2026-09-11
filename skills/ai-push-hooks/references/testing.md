# Testing And Troubleshooting

## Verify A Consuming Repository

1. Run `ai-push-hooks --help`; the CLI has `init`, `install`, and `hook`, not dedicated `test`, `doctor`, or `uninstall` commands.
2. Inspect `ai-push-hooks.toml`, the selected CLI/model, effective hook path (`git rev-parse --git-path hooks/pre-push`), and hook-manager stdin forwarding. Account for environment overrides and disabled/gated modules.
3. Validate workflow changes in a disposable repository with synthetic data. Test both a passing case and a deliberate failing assertion; confirm nonzero status reaches the caller. Use a deterministic-only workflow first to avoid model calls.
4. Only with approval, repeat using the real runner. Inspect logs/summaries and any edits. A passing manual hook invocation does not prove the manager forwards stdin correctly; exercise that integration against a disposable local bare remote, never the production remote.

To invoke the hook without a network push, supply Git's pre-push stdin protocol. Replace all placeholders with actual refs/object IDs from the disposable fixture:

```bash
printf '%s\n' \
  'refs/heads/main <local-sha> refs/heads/main <remote-sha>' | \
  ai-push-hooks hook origin <remote-url>
```

Each line is `<local-ref> <local-oid> <remote-ref> <remote-oid>`. A new remote ref uses the all-zero object ID of the repository's hash width. Use existing commits for normal old/new tips. This runs the workflow, including AI calls, edits, commands, and optional integrations; it is not a dry-run flag. Inspect configured side effects first. For apply, push exactly the checked-out `HEAD` on a single branch. Multiple simultaneous non-deletion branch updates fail closed; tags/deletions do not select a branch.

## Source Contributor Checks

These commands require an ai-push-hooks source checkout, not just the installed skill/package. Run tests only against disposable fixtures, never real remotes, credentials, PRs, or Beads data.

```bash
# Unit/integration suite.
uv run --no-project --with pytest pytest tests -q

# Distribution checks; require the checkout's Node/npm toolchain too.
uv run --no-project --with build python -m build
uv run --no-project --with twine python -m twine check dist/*
npm run test:npm-pack

# Installed-package end-to-end coverage (with test dependencies installed).
python -m pytest -q tests/test_installed_hook_e2e.py

# Real OpenCode CLI with loopback mock provider; requires working Docker.
bash scripts/opencode-contract-smoke.sh
```

For pinned validation tools: `python -m pip install -c constraints-ci.txt build pytest ruff twine`, then `python -m pytest -q` and `ruff check .` in an isolated development environment. Build into a new empty artifact directory when validating a release to avoid checking stale distributions.

The Docker smoke gate uses no external model calls and does not mount host credentials or the repository at runtime. A missing Docker daemon is a blocker, not a pass. Live provider probes are separate, opt-in, potentially billable, and disclose supplied content; do not enable `AI_PUSH_HOOKS_LIVE_PROBE` without approval. Report which checks actually ran, exit statuses, and blocked/skipped coverage.

## Diagnose Failures

Default runtime data is under `.git/ai-push-hooks/`: `logs/`, `summaries/`, OpenCode `transcripts/`, and run artifacts. Respect configured logging paths. Inspect locally; redact repository content, credentials, and provider responses before sharing.

| Symptom | Action |
| --- | --- |
| Command or Python missing | Confirm install surface and hook process `PATH`; npm still needs Python 3.10+ (documented support: 3.10-3.13). |
| Missing configuration | Run `init` only if absent; preserve existing config. |
| Hook absent/not invoked | Inspect `core.hooksPath`, effective pre-push file, executable status, and chosen manager; reinstall through that manager. |
| Existing hook refused | Integrate with its manager or preserve its delegate; do not default to `--force`. |
| Missing push context / ambiguous refs | Preserve stdin and both remote arguments; test a single branch update. |
| CLI auth/model failure | Authenticate the selected CLI in the hook's environment; choose an accessible model and check model overrides. |
| Unknown runner | Match selection to `[runners.<name>]`. |
| Runner capability error | Update the CLI to support adapter flags; rerun contract coverage. |
| Invalid JSON | Inspect prompt/schema and response artifact; default is two retries. Ask for JSON only and the exact payload shape. |
| Findings did not block | Add `assert`; check `when_env`, module enablement, skip/fail-open settings. |
| Apply refused | Verify single pushed branch equals `HEAD`, allowed paths, and protected/ignored/symlink exclusions. |
| Push blocked after edits | Inspect `git diff`, run tests, obtain approval to commit, then retry. |
| Need detail | Set `AI_PUSH_HOOKS_LOG_LEVEL=debug` for a controlled rerun; response printing is a separate sensitive opt-in. |

For a bug report include package/install surface, OS and tool versions, sanitized config/command/stdin shape, expected vs actual exit status, and a minimal disposable reproduction. Bypassing the hook is not a fix or evidence of passing validation.
