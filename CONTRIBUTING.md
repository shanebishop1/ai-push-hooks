# Contributing

Thanks for contributing to ai-push-hooks.

## Before opening a change

- Use an issue to discuss substantial features or behavior changes first.
- Use a [private security advisory](https://github.com/shanebishop1/ai-push-hooks/security/advisories/new), not an issue, for vulnerabilities.
- Keep changes focused and preserve compatibility with Python 3.10–3.13 and Node.js 18+ for the npm wrapper.
- Never include provider credentials, private repository content, generated transcripts, or `.git/ai-push-hooks/` runtime data.

## Development

From a clone with Python and [uv](https://docs.astral.sh/uv/) installed:

```bash
uv run --no-project --with pytest pytest tests -q
```

### Beads repository maintenance

Repository task state uses the native Beads `bd` CLI with embedded Dolt. Do
not substitute Beads-Rust (`br`). Schema migration is an explicit operator
maintenance task, not part of normal hook execution: freeze Beads writers,
make and checksum a cold full `.beads` backup outside the repository, rehearse
the pinned native `bd` migration on a disposable copy, and verify schema,
semantic records and relations, memories, and a clean Dolt working set before
touching live data. A local migration does not authorize `bd dolt push`, sync,
remote/ref changes, or independent migration by another clone.

Never export migration bypasses such as `BD_ALLOW_REMOTE_MIGRATE`,
`BD_IGNORE_SCHEMA_SKEW`, or `BD_SMART_GATE` into ordinary development or hook
environments. The Beads alignment executor strips them and permits only the
documented `bd update` and `bd close` forms. Continue to run tests only against
disposable repositories, never the live `.beads` store.

Validate both distribution surfaces before submitting package or wrapper changes:

```bash
uv run --no-project --with build python -m build
uv run --no-project --with twine python -m twine check dist/*
npm run test:npm-pack
```

### Beta gate: real OpenCode contract smoke test

Run the beta gate from a machine with a working Docker daemon:

```bash
bash scripts/opencode-contract-smoke.sh
```

The script pins and reports OpenCode `1.18.29`, builds the checked-out runtime
source into a disposable image, and creates the Git repository only inside the
container. Runtime networking is disabled except for the in-process loopback
mock provider; no host home, credentials, SSH agent, repository, or Docker
socket is mounted. The test uses no external model calls. It proves against
the real installed OpenCode CLI that the read-only agent cannot use file,
shell, task, or network tools; an allowlisted synthetic `README.md` edit is
propagated; outside-allowlist edits do not propagate; and protected Git
metadata is unchanged.

This is a contract smoke test, not an operating-system sandbox claim. Docker
must be available for a real result; a missing daemon is a blocker rather than
a passing mocked/unit-test substitute. The synthetic repository and all test
state are deleted when the container exits.

OpenCode `1.18.29` exposes separate `write` and `edit` tools, but both request
the `edit` permission using the worktree-relative target path. The runtime must
therefore keep its mutation allowlist under `edit`; do not add a broader
`write` permission. The gate deliberately requests the `write` tool so this
version-specific mapping remains covered.

### Reproducible validation

The checked-in `constraints-ci.txt` pins the validation tools. In a disposable
development environment, install those pins and run the exact candidate gate:

```bash
python -m pip install -c constraints-ci.txt build pytest ruff twine
python -m pytest -q
ruff check .
python -m build --outdir "$ARTIFACT_DIR"
python -m twine check "$ARTIFACT_DIR"/*
npm run test:npm-pack
python -m pytest -q tests/test_installed_hook_e2e.py
```

Set `ARTIFACT_DIR` to a newly created empty directory outside `dist/` and
`build/`. The installed-hook tests build a wheel and pack npm into owned temp
directories, clear source `PYTHONPATH`, use a minimal PATH, and test a local
bare remote. Do not run tests against the real repository's remotes, provider
credentials, transcripts, GitHub PRs, or Beads database. The no-network
OpenCode fixture is the `scripts/opencode-contract-smoke.sh` gate above; a
missing Docker daemon is blocked evidence, not a pass.

### Ownership, compatibility, and bug reports

Keep changes focused. Runtime, workflow, and test changes need an owner who
can explain the affected contract; documentation changes should identify the
user-visible behavior and exact validation. A reviewer should check failure
semantics, provider/data disclosure, path boundaries, and the Python 3.10–3.13
and Node.js 18+ compatibility policy before approval. Do not broaden support
claims from one provider, model, OS, or interpreter run. Lefthook remains the
full-featured repository hook-manager alternative; validate its effective hook
path and stdin forwarding in the consuming repository rather than replacing it
with a hand-written imitation.

For a reproducible bug report, include:

1. package/source version and installation surface (wheel, npm, or checkout);
2. OS, architecture, Python, Node/npm, Git, OpenCode, and hook-manager versions;
3. sanitized `ai-push-hooks.toml`, hook command/manager configuration, and the
   exact command plus sanitized pre-push stdin shape;
4. expected versus actual exit status and behavior, relevant logs with secrets,
   repository content, and transcript data removed; and
5. a minimal disposable fixture or reproduction steps. Report vulnerabilities
   privately through the security-advisory route instead of an issue.

Update documentation and `CHANGELOG.md` for user-visible behavior. A pull request should explain the motivation and security/compatibility impact and list exact validation commands and results. By contributing, you agree that your contribution is licensed under the repository's MIT license.
