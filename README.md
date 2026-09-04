# ai-push-hooks

`ai-push-hooks` catches repository drift before it reaches a remote. It turns `git push` into a configurable workflow that can inspect the exact outgoing diff, ask an LLM for structured findings, apply narrowly allowlisted documentation fixes, run deterministic actions, and block the push until changes are reviewed and committed.

Use it to keep docs aligned with code, check branch/task consistency, or prepare pull requests without replacing your project's ordinary lint, test, and build checks. Workflows are assembled from `collect`, `llm`, `apply`, `exec`, and `assert` steps and default to failing closed.

## Prerequisites

- [Python 3.10–3.13](https://www.python.org/downloads/). The npm package wraps the Python CLI, so Python is required for npm/pnpm installations too. On Python 3.10, npm-only installs also require `tomli` to be installed.
- [OpenCode](https://opencode.ai/docs/#install) plus an authenticated model provider for `llm` and `apply` steps. Follow OpenCode's [provider authentication guide](https://opencode.ai/docs/providers/) and confirm credentials with `opencode auth list`.
- [Lefthook](https://lefthook.dev/installation/) for the repository-owned pre-push integration shown below.
- [Mise](https://mise.jdx.dev/getting-started.html) for the recommended pinned installation below.
- [GitHub CLI (`gh`)](https://cli.github.com/manual/installation) only when using PR creation via `gh_pr_create`.

## Install ai-push-hooks

### Mise (recommended for repository hooks)

Pin the currently published release in the consuming repository:

```bash
mise use npm:ai-push-hooks@0.1.19
```

This adds the following project-level tool entry to `mise.toml` and installs it:

```toml
[tools]
"npm:ai-push-hooks" = "0.1.19"
```

After checking in `mise.toml`, other contributors can install the pinned tool with `mise install`.

### Python

```bash
uv tool install ai-push-hooks
# or
pipx install ai-push-hooks
```

### Node (wrapper around Python package)

```bash
npm install --save-dev ai-push-hooks
# or
pnpm add -D ai-push-hooks
```

### OpenCode isolation limits

OpenCode runs in `--pure` mode with project configuration disabled, isolated home/config/cache/state directories, sharing disabled, and an ai-push-hooks-owned custom agent configuration. Read-only steps run in an empty scratch directory, receive only hook-owned artifacts through `--file`, and have every tool denied. Apply steps run against a private temporary workspace containing only unignored regular files matching `allow_paths`; their agent permits only reads and allowlisted edits in that workspace. Casefolded, Unicode-normalized `.git` and `AGENTS.md` paths are always protected.

Global and repository OpenCode instructions, custom agents, MCP servers, formatters, LSP configuration, sharing, and plugins are not inherited. The existing XDG data directory is retained for OpenCode authentication/session state, and recognized provider environment variables are forwarded. Custom providers defined only in global OpenCode configuration are therefore unsupported; use a built-in provider with OpenCode auth state or environment credentials.

After OpenCode session finalization, apply verifies that the Git-visible checkout, index, current-worktree control state, and critical shared `HEAD`/config/packed-refs/refs/hooks state still match their baselines. Pre-existing symlinks in monitored Git metadata fail closed before OpenCode runs, and symlinks introduced during execution fail before propagation. Apply then preflights every destination against its exact baseline type, content digest, and mode before propagating anything, performs atomic file replacement, and verifies the resulting checkout and protected Git state again. Safe existing ordinary `rwx` modes are preserved, existing special bits are stripped, new or group/world-writable modes become owner-only, and staged files carrying setuid/setgid/sticky bits are rejected before any propagation. Hook-owned runtime files default to `0600` and runtime directories to `0700`.

These controls are OpenCode permission and workspace isolation, not an operating-system sandbox. Compare-and-swap preflight minimizes lost updates but cannot make the interval between preflight and filesystem replacement atomic against an independent local process. Ignored worktree trees, Git object/LFS stores, shared reflogs, and metadata belonging only to other linked worktrees are intentionally excluded from bounded snapshots; direct changes there may not be detected. Critical shared refs/config/hooks remain monitored. Automatic rollback is avoided so pre-existing user changes are not overwritten.

## Quick start

1. Install and authenticate the prerequisites, then add the pinned Mise tool by following [Mise installation](#mise-recommended-for-repository-hooks) above. Verify the CLIs:

   ```bash
   mise exec -- ai-push-hooks --help
   opencode auth list
   lefthook version
   ```

2. Generate a starter config:

   ```bash
   mise exec -- ai-push-hooks init --template minimal-docs
   ```

3. Add the single repository-owned runner at `scripts/hooks/pre-push-runner.sh`:

   ```bash
   #!/usr/bin/env bash
   set -euo pipefail

   remote_name="${1:-}"
   remote_url="${2:-}"
   push_stdin="$(mktemp)"
   trap 'rm -f "$push_stdin"' EXIT
   cat >"$push_stdin"

   # Run deterministic quality checks first. This Git-native check works in any repo;
   # add the repository's lint, test, and build commands here too.
   git diff --check

   # Keep ai-push-hooks as the single final phase and replay Git's pre-push input.
   mise exec -- ai-push-hooks hook "$remote_name" "$remote_url" <"$push_stdin"
   ```

   Make the runner executable:

   ```bash
   chmod +x scripts/hooks/pre-push-runner.sh
   ```

4. Configure Lefthook to invoke only that runner in `lefthook.yml`:

   ```yaml
   pre-push:
     commands:
       repository-pre-push:
         run: bash scripts/hooks/pre-push-runner.sh {1} {2}
         use_stdin: true
   ```

   `use_stdin: true` forwards Git's ref-update stream to the runner. The runner captures it before quality checks consume or close standard input, then replays it to `ai-push-hooks hook`. Lefthook's `{1}` and `{2}` are the remote name and remote URL. Keep all repository checks in this runner and keep the one `ai-push-hooks hook` call last so failures propagate and block the push.
5. Install and verify the hook:

   ```bash
   lefthook install
   test -x "$(git rev-parse --git-path hooks/pre-push)" && echo "pre-push hook installed"
   ```

6. Configure modules and steps in the [configuration reference](#configuration-reference).
7. Push as usual. The hook derives context from Git's outgoing ref updates and runs modules in configured order; workflow failures and failed assertions block the push by default. If an `apply` step edits an allowlisted file, the starter workflow's assertion blocks that push so you can inspect `git diff`, run your checks, commit the approved edit, and push again. The second push evaluates the new commit rather than silently pushing unreviewed model output.

## Troubleshooting

- **`opencode is required but not installed`:** install OpenCode and ensure `opencode` (or `opencode-cli`) is on `PATH` for the Git hook process.
- **Provider/model authentication fails:** run `opencode auth list`, authenticate a built-in provider, and verify `[llm].model`. Project/global custom-provider configuration is intentionally not loaded; see [OpenCode isolation limits](#opencode-isolation-limits).
- **The hook does not run:** rerun `lefthook install`, check `git config --get core.hooksPath`, and verify the pre-push path with the command above.
- **The push is blocked after docs changed:** this is the expected edit-review-commit flow. Review `git diff`, validate and commit the changes, then push again.
- **Find logs or transcripts:** inspect `.git/ai-push-hooks/logs`, `.git/ai-push-hooks/summaries`, and (when enabled) `.git/ai-push-hooks/transcripts`.
- **Temporarily skip intentionally:** set `AI_PUSH_HOOKS_SKIP=1` for one invocation. Treat bypasses as an explicit project-policy decision.

## Security and privacy

Repository diffs, selected context, and prompts may be sent by OpenCode to the configured model provider. Review that provider's data-handling terms and do not include secrets in commits or prompts. Transcripts are stored locally by default under `.git/ai-push-hooks/transcripts`; sharing is disabled and OpenCode sessions are deleted after each run by default. See [SECURITY.md](SECURITY.md) for reporting, the threat model, data handling, and sandbox limitations.

## Commands

If installed as a local npm/pnpm dependency, run commands with `npx` or `pnpm exec`.

| Command | What it does |
| --- | --- |
| `ai-push-hooks hook <remote-name> <remote-url>` | Runs the configured pre-push workflow. |
| `ai-push-hooks init --template minimal-docs` | Writes `ai-push-hooks.toml` starter config. |
| `ai-push-hooks init --template minimal-docs --force` | Overwrites an existing config file. |

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
