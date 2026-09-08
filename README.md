# ai-push-hooks

`ai-push-hooks` catches repository drift before it reaches a remote. It turns `git push` into a configurable workflow that can inspect the exact outgoing diff, ask an LLM for structured findings, apply narrowly allowlisted documentation fixes, run deterministic actions, and block the push until changes are reviewed and committed.

Use it to keep docs aligned with code, check branch/task consistency, or prepare pull requests without replacing your project's ordinary lint, test, and build checks. Workflows are assembled from `collect`, `llm`, `apply`, `exec`, and `assert` steps and default to failing closed.

## Quick start: repo-local hook

### Prerequisites

- [Git](https://git-scm.com/downloads) and a POSIX shell for the generated hook.
- [Python 3.10–3.13](https://www.python.org/downloads/). Python is required even when installing the npm wrapper. The wrapper probes Python 3.14, 3.13, 3.12, 3.11, 3.10, then `python`; the 3.14 probe is not a beta support claim. Python 3.10 additionally needs the `tomli` package available to that interpreter.
- [OpenCode](https://opencode.ai/docs/#install) is optional for workflows that use only deterministic steps, but the `minimal-docs` starter uses `llm` and `apply`. Those steps also need a provider/model and authentication; check with `opencode auth list`.
- [GitHub CLI (`gh`)](https://cli.github.com/manual/installation) is optional and needed only for `gh_pr_create`.
- [Beads (`bd`)](https://github.com/steveyegge/beads) is optional and needed only for Beads alignment steps. The integration requires the native `bd` CLI; Beads-Rust (`br`) is not a supported substitute.
- [Lefthook](https://lefthook.dev/installation/) and [Mise](https://mise.jdx.dev/getting-started.html) are optional hook-manager/tool-version alternatives described below.

### Install ai-push-hooks

This is the shortest path. It installs the package, writes the exact starter
configuration filename, and installs a repository-local `pre-push` delegate:

```bash
python -m pip install ai-push-hooks==0.2.1
ai-push-hooks init --template minimal-docs
ai-push-hooks install
```

The installed-artifact tests exercise the equivalent install and hook sequence
with wheel and npm artifacts in disposable repositories, including a real
local push that succeeds and a second push that is rejected without changing
the bare remote. The first command above can instead be `uv tool install
ai-push-hooks` or `pipx install ai-push-hooks` when using an isolated
application environment.

These instructions describe the `0.2.1` beta release. npm exposes it through
the `beta` dist-tag; PyPI has no separate beta channel, so Python installation
must select the exact `0.2.1` version. Published `0.1.19` artifacts retain
historical provenance and must not be assumed to contain this release's
`install` command.

The starter config currently writes `openai/gpt-5.6-terra` as its model value;
availability and authentication are provider-dependent. To use a free OpenCode
Zen model, run `opencode models opencode`, choose a model currently marked
free, and set `[llm].model` to that full identifier. Free-model availability
changes over time, so do not treat the model used in the recorded preview below
as a permanent recommendation or default.

For npm or pnpm, install and invoke the wrapper locally:

```bash
npm install --save-dev ai-push-hooks@beta
npx --no-install ai-push-hooks init --template minimal-docs
npx --no-install ai-push-hooks install
# or: pnpm add -D ai-push-hooks && pnpm exec ai-push-hooks install
```

Use `ai-push-hooks@0.2.1` instead of `@beta` when an exact npm version pin is
required.

The npm package does not contain a Python runtime. Ensure the Python
requirement above is on `PATH` for the hook process; on Python 3.10 install
`tomli` in that same environment. `npx --no-install` avoids an accidental
registry lookup or global-package fallback.

`install` resolves Git's effective hook path without changing Git
configuration. It accepts `install [--force]`, creates missing repository-local
hook directories, writes atomically, and makes the delegate executable. An
existing regular hook is refused unless `--force` is explicit; symlinks,
reparse points, FIFOs, directories, external/shared `core.hooksPath` values,
and linked-worktree shared hooks are refused even with `--force`. `--force`
replaces a regular existing hook; it does not merge or back it up. Inspect a
hook before using it and keep a copy if it contains work you need.

The generated delegate preserves Git's hook arguments, standard input, and
exit status. When installed through the local npm wrapper, it records the
absolute Node and package-script paths, so Git does not need
`node_modules/.bin` on `PATH`; keep that local package installation in place.
Python console installs similarly record their executable when it can be
resolved. The fallback delegate fails clearly with status 127 if
`ai-push-hooks` is not on the hook process's `PATH`.

### Mise (pinned tool option)

Pin an approved published release in the consuming repository:

```bash
mise use npm:ai-push-hooks@0.2.1
```

This adds the following project-level tool entry to `mise.toml` and installs it:

```toml
[tools]
"npm:ai-push-hooks" = "0.2.1"
```

After checking in `mise.toml`, other contributors can install the pinned tool with `mise install`.

### OpenCode isolation limits

OpenCode runs in `--pure` mode with project configuration disabled, isolated home/config/cache/state directories, sharing disabled, and an ai-push-hooks-owned custom agent configuration. Read-only steps run in an empty scratch directory, receive only hook-owned artifacts through `--file`, and have every tool denied. Apply steps run against a private temporary workspace containing only unignored regular files matching `allow_paths`; their agent permits only reads and allowlisted edits in that workspace. Casefolded, Unicode-normalized `.git` and `AGENTS.md` paths are always protected.

Built-in OpenCode plugins remain enabled, including built-in authentication plugins such as Codex OAuth. Normal `--pure` execution disables external plugins, while the hook's empty plugin configuration and project-config disablement prevent project and global plugins and configuration from being inherited. The existing XDG data directory is retained for OpenCode authentication/session state, and recognized provider environment variables, including `OPENAI_API_KEY`, are forwarded; OpenCode itself chooses the authentication path using its normal precedence. Custom providers defined only in global OpenCode configuration are therefore unsupported; use a built-in provider with OpenCode auth state or environment credentials.

After OpenCode session finalization, apply verifies that the Git-visible checkout, index, current-worktree control state, and critical shared `HEAD`/config/packed-refs/refs/hooks state still match their baselines. Pre-existing symlinks in monitored Git metadata fail closed before OpenCode runs, and symlinks introduced during execution fail before propagation. Apply then preflights every destination against its exact baseline type, content digest, and mode before propagating anything, performs atomic file replacement, and verifies the resulting checkout and protected Git state again. Safe existing ordinary `rwx` modes are preserved, existing special bits are stripped, new or group/world-writable modes become owner-only, and staged files carrying setuid/setgid/sticky bits are rejected before any propagation. Hook-owned runtime files default to `0600` and runtime directories to `0700`.

These controls are OpenCode permission and workspace isolation, not an operating-system sandbox. Compare-and-swap preflight minimizes lost updates but cannot make the interval between preflight and filesystem replacement atomic against an independent local process. Ignored worktree trees, Git object/LFS stores, shared reflogs, and metadata belonging only to other linked worktrees are intentionally excluded from bounded snapshots; direct changes there may not be detected. Critical shared refs/config/hooks remain monitored. Automatic rollback is avoided so pre-existing user changes are not overwritten.

### Beads maintenance boundary

The `beads_alignment` executor is for ordinary native `bd update` and `bd close`
operations only. It does not run migrations, synchronize embedded Dolt, or
publish Dolt refs. Hook-launched Beads commands also discard
`BD_ALLOW_REMOTE_MIGRATE`, `BD_IGNORE_SCHEMA_SKEW`, and `BD_SMART_GATE` from
their inherited environment so an operator maintenance override cannot leak
into a push hook.

Treat a Beads schema migration as separate operator maintenance. Pin and
verify the native `bd` version, stop Beads writers and hooks, take a cold full
backup of `.beads`, and rehearse against a disposable copy before opening the
live embedded-Dolt store. Verify the schema, semantic issue/dependency data,
memories, and a clean Dolt working set before and after the live cutover.
Remote publication, including `bd dolt push`, is a separate explicit action;
a successful local migration does not authorize it. Do not replace this flow
with Beads-Rust (`br`).

## Full-featured alternative: Lefthook

Use Lefthook when the repository needs several hook commands, shared hook
configuration, or repository-managed installation. Keep one final
`ai-push-hooks hook` call and forward Git's pre-push input:

Create `scripts/hooks/pre-push-runner.sh` in the consuming repository with:

```yaml
pre-push:
  commands:
    repository-pre-push:
      run: bash scripts/hooks/pre-push-runner.sh {1} {2}
      use_stdin: true
```

The runner captures stdin before deterministic checks consume it, then replays
it to the tool:

```bash
#!/usr/bin/env bash
set -euo pipefail
remote_name="${1:-}"
remote_url="${2:-}"
push_stdin="$(mktemp)"
trap 'rm -f "$push_stdin"' EXIT
cat >"$push_stdin"
git diff --check
mise exec -- ai-push-hooks hook "$remote_name" "$remote_url" <"$push_stdin"
```

Install and verify Lefthook in the consuming repository:

```bash
lefthook version
chmod +x scripts/hooks/pre-push-runner.sh
lefthook install
test -x "$(git rev-parse --git-path hooks/pre-push)" && echo "pre-push hook installed"
```

`use_stdin: true` forwards Git's ref-update stream; `{1}` and `{2}` are the
remote name and URL. This is the full-featured alternative to the small
repo-local `install` delegate. Do not install both managers for the same hook
unless their chaining is deliberate. The repository's deterministic installed
hook coverage validates the delegate contract; Lefthook remains the tested
repository-owned integration pattern and should be exercised with the commands
above in each consuming repository.

Configure modules and steps in the [configuration reference](#configuration-reference), then push as usual. If `apply` edits an allowlisted file, the starter assertion blocks that push so you can inspect `git diff`, validate, commit the approved edit, and push again.

## Troubleshooting

- **`opencode is required but not installed`:** install OpenCode and ensure `opencode` (or `opencode-cli`) is on `PATH` for the Git hook process.
- **Provider/model authentication fails:** run `opencode auth list`, authenticate a built-in provider, and verify `[llm].model`. Built-in auth plugins remain available, while project/global custom-provider configuration is intentionally not loaded. Recognized provider environment variables, including `OPENAI_API_KEY`, are forwarded and OpenCode chooses authentication. See [OpenCode isolation limits](#opencode-isolation-limits).
- **The hook does not run:** rerun `lefthook install`, check `git config --get core.hooksPath`, and verify the pre-push path with the command above.
- **The push is blocked after docs changed:** this is the expected edit-review-commit flow. Review `git diff`, validate and commit the changes, then push again.
- **Find logs or transcripts:** inspect `.git/ai-push-hooks/logs`, `.git/ai-push-hooks/summaries`, and (when enabled) `.git/ai-push-hooks/transcripts`.
- **Temporarily skip intentionally:** set `AI_PUSH_HOOKS_SKIP=1` for one invocation. Treat bypasses as an explicit project-policy decision.

### Failure, fail-open, and skip semantics

- Fail closed is the default: configuration, collection, model, apply, exec,
  and assertion errors return nonzero and block the push. A rejected push does
  not update the remote.
- Set `[general].allow_push_on_error = true`, or use
  `AI_PUSH_HOOKS_ALLOW_PUSH_ON_ERROR=1`, only as an explicit fail-open policy.
  The error is still logged and a warning is emitted, but the push is allowed.
- `AI_PUSH_HOOKS_SKIP=1` exits before repository/config access and allows that
  invocation. `general.enabled = false` is a configured disable after config
  loading/log initialization. Neither path validates or runs the workflow.
- `skip_on_sync_branch = true` can skip configured sync-branch context, and
  step/module conditions can report a normal skip. A skip is not a successful
  model or apply result.

Git supplies hook stdin as one line per ref update:
`<local-ref> <local-object-id> <remote-ref> <remote-object-id>`. New remote
refs use a zero object ID (40 zeroes for the tested SHA-1 repositories); deletion
uses `(delete)` and a zero local ID. The hook rejects malformed input, unknown
objects, and multiple non-deleted branch updates rather than silently choosing
a branch.

### Safe removal of the generated hook

`install` has no uninstall command and never edits Git configuration. To remove
only its delegate, first inspect the effective target and content:

```bash
hook="$(git rev-parse --git-path hooks)/pre-push"
if [ -f "$hook" ]; then sed -n '1,20p' "$hook"; fi
```

For an exact check before removal, use the installed Python package rather than
matching only a filename:

```bash
python - "$hook" <<'PY'
import pathlib
import sys

from ai_push_hooks.install import pre_push_hook_script

path = pathlib.Path(sys.argv[1])
if not path.is_file() or path.is_symlink():
    raise SystemExit("not a regular hook; nothing removed")
if path.read_text(encoding="utf-8") != pre_push_hook_script():
    raise SystemExit("hook is not the ai-push-hooks delegate; nothing removed")
path.unlink()
print(f"removed {path}")
PY
```

Remove it only after confirming it is the generated `ai-push-hooks` delegate,
not a Lefthook or shared hook. If it contains other commands, preserve it and
remove only the documented ai-push-hooks entry instead. Do not run `rm` on an
uninspected `pre-push` path.

## Security and privacy

Repository diffs, selected context, and prompts may be sent by OpenCode to the configured model provider. Review that provider's data-handling terms and do not include secrets in commits or prompts. Transcripts are stored locally by default under `.git/ai-push-hooks/transcripts`; sharing is disabled and OpenCode sessions are deleted after each run by default. If transcript export fails, the run warns and still follows the configured deletion policy; do not assume an export exists. See [SECURITY.md](SECURITY.md) for reporting, the threat model, data handling, and sandbox limitations.

### BR-06 provider evidence (limited preview)

At the time of the recorded synthetic provider run, OpenCode **1.18.29** listed
`opencode/muse-spark-1.3-contributor-free` as free. Synthetic `query` and
`analyze` steps passed at **zero reported cost**. This is evidence for that
specific OpenCode/model path and synthetic inputs only; it does not establish
that every model, provider, authentication mode, or live `apply` operation is
compatible. Discover the current catalog with `opencode models opencode` and
verify pricing before each run; free models can be renamed, replaced, or
removed. The tested identifier is historical evidence, not a new default or a
promise of future availability. Review provider billing, retention, and
transmission terms before using repository content.

## Tested matrix and beta boundary

The current candidate was exercised on **macOS Darwin 24.6.0 arm64** with
Python **3.12.13**, Node **24.19.0**, npm **10.9.2**, Git **2.55.0**, Ruff
**0.13.3**, OpenCode **1.18.29**, `gh` **2.93.0**, and `bd` **1.2.2**. The
wheel and packed npm installed-hook tests use disposable repositories, full
40-character Git object IDs, a local bare remote, and a minimal PATH. The
Lefthook **2.1.9** was also run in a disposable repository to install a
pre-push hook and verify argument/stdin forwarding. The real OpenCode contract
also passed in a Linux arm64 Docker container launched from this macOS host.
Python 3.10, 3.11, and 3.13 and Node 18 were not available in this validation
environment and are not claimed as locally run; their jobs remain part of the
GitHub Actions matrix.

Prior recorded BR evidence also covers the real OpenCode 1.18.29 CLI with a
loopback mock provider inside a Linux Docker runtime with networking disabled.
That is mock-provider permission/workspace evidence, not live-provider or
operating-system-sandbox evidence.

Validation results for this snapshot are **273 passed, 1 skipped** for the full
Python suite, **8 passed** for install-unit coverage, **2 passed** for installed
wheel and npm hook coverage against the exact release artifacts, a passing
`npm run test:npm-pack`, and a passing Lefthook 2.1.9 disposable
argument/stdin-forwarding check. The Docker contract passed against the real
OpenCode 1.18.29 CLI with runtime networking disabled and a loopback mock
provider.

Python 3.10–3.13 and Node 18+ remain the declared compatibility ranges, not a
claim that every patch/platform combination has passed. Windows has no native
beta evidence and is explicitly untested/not supported for this beta. The
generated hook and documented runner require a POSIX shell; defensive path
handling is not Windows validation.

## Synthetic demo and evidence

For a no-secrets, no-external-model-call wiring/permission demo, run:

```bash
bash scripts/opencode-contract-smoke.sh
```

It builds a disposable image, starts only an in-process loopback mock provider,
and drives real OpenCode 1.18.29 through a synthetic repository. Runtime
networking is disabled, so no external model call is possible; the initial
Docker build/setup may need network access to fetch its pinned inputs. It shows
the allowlisted `README.md` edit, denied outside/protected edits, and unchanged
protected Git metadata. This is **wiring and permission evidence only**, not a
live-provider demo or OS-sandbox claim; it exits 2 when Docker is unavailable.

For installed hook wiring without any model/provider call:

```bash
python -m pytest -q tests/test_installed_hook_e2e.py
npm run test:npm-pack
```

These disposable fixtures show a successful local push followed by a
fail-closed rejection. The actual BR-06 provider preview is documented above;
it used synthetic query/analyze inputs and must not be presented as fabricated
provider output or as evidence for live `apply`.

## Portfolio case study: bounded documentation maintenance

**Problem.** A pushed code change can make repository documentation stale
before review notices it. The hook inspects the outgoing ref range rather than
the checked-out branch alone.

**Architecture and tradeoffs.** Deterministic `collect` steps establish diff,
changed-file, and repository context before `llm` query/analyze steps. An
`apply` step receives a private workspace and a narrow docs allowlist; the
assertion then blocks the push for human review and commit. `exec` and `assert`
remain available for deterministic repository actions. This ordering limits
model scope without pretending to provide an OS sandbox.

**Evidence.** The real OpenCode 1.18.29 mock-provider contract found a
version-specific permission mapping (`write` requests `edit`) and covers
allowlisted propagation plus protected Git metadata. The BR-06 live synthetic
preview used `opencode/muse-spark-1.3-contributor-free` for query/analyze at
zero reported cost; it is not evidence for all providers/models or live apply.
Installed wheel/npm tests cover hook wiring, a successful local push, and a
fail-closed rejection. The Docker contract was not rerun in this environment.

**Limitations.** Provider availability, billing, retention, OS-level access,
ignored trees, shared metadata, and independent filesystem races remain outside
the product guarantee. Review, validate, and commit any proposed edit; this is
not unattended autonomous maintenance.

## Commands

If installed as a local npm/pnpm dependency, run commands with `npx --no-install` or `pnpm exec`.

| Command | What it does |
| --- | --- |
| `ai-push-hooks hook <remote-name> <remote-url>` | Runs the configured pre-push workflow. |
| `ai-push-hooks init --template minimal-docs` | Writes `ai-push-hooks.toml` starter config. |
| `ai-push-hooks init --template minimal-docs --force` | Overwrites an existing config file. |
| `ai-push-hooks install [--force]` | Installs a repo-local executable pre-push delegate; `--force` replaces a regular existing hook. |

## Configuration overview

- Config file: `ai-push-hooks.toml` in repo root (required).
- Prompt resolution precedence for `llm` and `apply` steps:
  1. `prompt`
  2. `prompt_file`
  3. `fallback_prompt_id`

## Configuration reference

### Top-level keys

| Key | Type | Required | Default |
| --- | --- | --- | --- |
| `general` | table | no | see section defaults |
| `llm` | table | no | see section defaults |
| `logging` | table | no | see section defaults |
| `workflow` | table | yes | n/a |
| `modules` | table | yes | n/a |

### `[general]`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | Enables or disables the hook globally. |
| `allow_push_on_error` | bool | `false` | If `true`, push continues even when workflow fails. |
| `require_clean_worktree` | bool | `false` | If `true`, aborts when local changes exist. |
| `skip_on_sync_branch` | bool | `true` | If `true`, skips on sync branch/worktree context. |
| `base_branch` | string | `"main"` | Base branch used for new-branch range fallback and default PR base/context. |

### `[llm]`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `runner` | string | `"opencode"` | LLM runner label (currently OpenCode flow). |
| `model` | string | `"openai/gpt-5.6-terra"` | Model passed to OpenCode. |
| `variant` | string | `""` | Optional OpenCode variant. |
| `timeout_seconds` | int | `800` | Timeout per LLM invocation and related OpenCode calls. |
| `max_parallel` | int | `2` | Max concurrent read-only steps (`collect`, `llm`). |
| `json_max_retries` | int | `2` | Retry count for invalid JSON responses. |
| `invalid_json_feedback_max_chars` | int | `6000` | Max invalid output included in retry feedback. |
| `json_retry_new_session` | bool | `true` | Starts a new OpenCode session on JSON retry. |
| `delete_session_after_run` | bool | `true` | Deletes OpenCode sessions after completion. |
| `max_diff_bytes` | int | `180000` | Max bytes of git diff sent into workflow artifacts. |
| `session_title_prefix` | string | `"ai-push-hooks"` | Prefix for OpenCode session titles. |

### `[logging]`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `level` | string | `"status"` | Console verbosity (`status`, `info`, `debug`). |
| `jsonl` | bool | `true` | Enables JSONL event logging. |
| `dir` | string | `".git/ai-push-hooks/logs"` | Directory for `hook.jsonl`. |
| `capture_llm_transcript` | bool | `true` | Exports OpenCode session transcripts. |
| `transcript_dir` | string | `".git/ai-push-hooks/transcripts"` | Transcript export directory. |
| `summary_dir` | string | `".git/ai-push-hooks/summaries"` | Per-run summary JSON directory. |
| `print_llm_output` | bool | `false` | Mirrors raw OpenCode JSON stream to stdout. |

### `[workflow]`

| Key | Type | Required | Description |
| --- | --- | --- | --- |
| `modules` | array of strings | yes | Ordered module IDs to run. Must contain at least one module and each ID must exist under `[modules]`. |

### `[modules.<module_id>]`

| Key | Type | Required | Description |
| --- | --- | --- | --- |
| `enabled` | bool | no | Enables or disables that module. Default `true`. |
| `steps` | array of step tables | yes | Ordered workflow steps for the module. Must be non-empty. |

### `[[modules.<module_id>.steps]]`

| Key | Type | Required | Applies to | Description |
| --- | --- | --- | --- | --- |
| `id` | string | yes | all step types | Unique step identifier inside the module. |
| `type` | string | yes | all step types | One of: `collect`, `llm`, `apply`, `exec`, `assert`. |
| `inputs` | array of strings | no | non-`collect` steps | Artifact references from earlier steps. |
| `output` | string | yes | `llm` | Output artifact filename (often `.json`). |
| `schema` | string | no | `llm` | Validates parsed model output shape. |
| `prompt` | string | conditional | `llm`, `apply` | Highest-priority prompt source. |
| `prompt_file` | string | conditional | `llm`, `apply` | Repo-relative prompt file path; absolute, traversing, and symlinked paths are rejected. |
| `fallback_prompt_id` | string | conditional | `llm`, `apply` | Built-in prompt ID used when no higher source resolves. |
| `collector` | string | yes | `collect` | Collector handler ID. |
| `allow_paths` | array of strings | yes | `apply` | File glob allowlist for edits. |
| `executor` | string | yes | `exec` | Exec handler ID. |
| `assertion` | string | yes | `assert` | Assertion handler ID. |
| `when_env` | string | no | any step | Runs step only when env var parses as true. |

`llm` and `apply` are promptable step types: at least one of `prompt`, `prompt_file`, or `fallback_prompt_id` must be set.

Artifact references in `inputs` are module-local. Use `<step>/<artifact>` to reference an artifact produced by an earlier step in the same module (for example, `collect/push.diff` or `analyze/issues.json`). Cross-module references such as `docs:collect/push.diff` are not currently supported.

### Supported handler and schema values

#### Collectors

| Value | Purpose |
| --- | --- |
| `docs_context` | Collects docs-related context and diff artifacts. |
| `beads_status_context` | Collects branch/beads alignment context. |
| `pr_context` | Collects PR composition context. |

#### LLM schemas

| Value | Expected payload |
| --- | --- |
| `string_array` | JSON array of strings. |
| `docs_issue_array` | JSON array of issue objects with at least `file` and `description`. |
| `beads_alignment_result` | JSON object, optionally with `commands` string array. |
| `pr_create_payload` | JSON object for PR creation fields. |

#### Exec handlers

| Value | Purpose |
| --- | --- |
| `beads_alignment` | Runs non-interactive Beads commands and writes action report when needed. |
| `gh_pr_create` | Creates (or reuses) a GitHub PR via `gh`. |

#### Assertion handlers

| Value | Purpose |
| --- | --- |
| `docs_apply_requires_manual_commit` | Fails when docs were auto-edited and still need user review/commit. |
| `beads_alignment_clean` | Fails when Beads alignment reports unresolved work. |

#### Built-in fallback prompt IDs

| Value | Purpose |
| --- | --- |
| `docs-query-basic` | Generate doc search queries from diff. |
| `docs-analysis-basic` | Identify factual documentation drift. |
| `docs-apply-basic` | Apply minimal doc fixes within allowlist. |
| `beads-plan-basic` | Build Beads alignment command/report payload. |
| `pr-compose-basic` | Draft PR title/body/base/head payload. |

## Environment variable overrides

Boolean env parsing accepts: `1`, `true`, `yes`, `y`, `on` and `0`, `false`, `no`, `n`, `off`.

| Env var | Effect |
| --- | --- |
| `AI_PUSH_HOOKS_SKIP` | If true, sets `general.enabled = false`. |
| `AI_PUSH_HOOKS_ALLOW_PUSH_ON_ERROR` | Overrides `general.allow_push_on_error`. |
| `AI_PUSH_HOOKS_REQUIRE_CLEAN` | Overrides `general.require_clean_worktree`. |
| `AI_PUSH_HOOKS_ALLOW_DIRTY` | If true, forces `general.require_clean_worktree = false`. |
| `AI_PUSH_HOOKS_BASE_BRANCH` | Overrides `general.base_branch`. |
| `AI_PUSH_HOOKS_LOG_LEVEL` | Overrides `logging.level`. |
| `AI_PUSH_HOOKS_PRINT_LLM_OUTPUT` | Overrides `logging.print_llm_output`. |
| `AI_PUSH_HOOKS_MODEL` | Overrides `llm.model`. |
| `AI_PUSH_HOOKS_VARIANT` | Overrides `llm.variant`. |
| `AI_PUSH_HOOKS_TIMEOUT_SECONDS` | Overrides `llm.timeout_seconds` (integer). |

`when_env` is step-level and can point to any env var. A common example is `AI_PUSH_HOOKS_CREATE_PR` to gate PR creation steps.

## Example: docs + PR with opt-in creation

```toml
[workflow]
modules = ["docs", "pr"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"

[[modules.docs.steps]]
id = "query"
type = "llm"
fallback_prompt_id = "docs-query-basic"
inputs = ["collect/push.diff", "collect/changed-files.txt"]
output = "queries.json"
schema = "string_array"

[[modules.docs.steps]]
id = "analyze"
type = "llm"
fallback_prompt_id = "docs-analysis-basic"
inputs = ["collect/push.diff", "collect/docs-context.txt", "query/queries.json", "collect/recent-commits.txt"]
output = "issues.json"
schema = "docs_issue_array"

[[modules.docs.steps]]
id = "apply"
type = "apply"
fallback_prompt_id = "docs-apply-basic"
inputs = ["collect/push.diff", "collect/docs-context.txt", "analyze/issues.json"]
allow_paths = ["README.md", "docs/**/*.md"]

[[modules.docs.steps]]
id = "assert"
type = "assert"
assertion = "docs_apply_requires_manual_commit"
inputs = ["apply/result.json"]

[modules.pr]
enabled = true

[[modules.pr.steps]]
id = "collect"
type = "collect"
collector = "pr_context"

[[modules.pr.steps]]
id = "compose"
type = "llm"
fallback_prompt_id = "pr-compose-basic"
inputs = ["collect/pr-context.txt", "collect/changed-files.txt", "collect/push.diff", "collect/commits.txt"]
output = "pr-draft.json"
schema = "pr_create_payload"

[[modules.pr.steps]]
id = "create"
type = "exec"
executor = "gh_pr_create"
when_env = "AI_PUSH_HOOKS_CREATE_PR"
inputs = ["compose/pr-draft.json"]
```
