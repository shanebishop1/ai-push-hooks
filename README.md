# ai-push-hooks

`ai-push-hooks` is a configurable pre-push workflow runner for local Git repositories.

It runs module-based steps (collect, LLM, apply, exec, assert) before push so you can automate checks and optional repo maintenance tasks.

## Install

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

Requirements:

- [Python 3.10+](https://www.python.org/downloads/) (`python3` or `python`) is required. The npm package is a wrapper around the Python CLI, so Python is still required when installed through npm/pnpm.
- [OpenCode](https://github.com/anomalyco/opencode) is required for `llm` and `apply` steps.
- [GitHub CLI (`gh`)](https://cli.github.com/manual/installation) is required only if you use PR creation via `gh_pr_create`.

## Quick start

1. Install by following the steps above.
2. Generate a starter config:

   Python tool install (`uv tool` / `pipx`):

   ```bash
   ai-push-hooks init --template minimal-docs
   ```

   npm/pnpm local install:

   ```bash
   npx ai-push-hooks init --template minimal-docs
   # or
   pnpm exec ai-push-hooks init --template minimal-docs
   ```

3. Configure modules and steps in [Configuration reference](#configuration-reference).
4. Wire it into your pre-push hook manager. Lefthook example:

   ```yaml
   pre-push:
     commands:
       ai-push-hooks:
         run: ai-push-hooks hook {1} {2}
   ```

   In Lefthook, `{1}` is the remote name and `{2}` is the remote URL from Git's `pre-push` hook args.

5. Push as usual. The workflow runs automatically before push completes.

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

### `[llm]`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `runner` | string | `"opencode"` | LLM runner label (currently OpenCode flow). |
| `model` | string | `"openai/gpt-5.3-codex"` | Model passed to OpenCode. |
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
| `prompt_file` | string | conditional | `llm`, `apply` | Repo-relative or absolute prompt file path. |
| `fallback_prompt_id` | string | conditional | `llm`, `apply` | Built-in prompt ID used when no higher source resolves. |
| `collector` | string | yes | `collect` | Collector handler ID. |
| `allow_paths` | array of strings | yes | `apply` | File glob allowlist for edits. |
| `executor` | string | yes | `exec` | Exec handler ID. |
| `assertion` | string | yes | `assert` | Assertion handler ID. |
| `when_env` | string | no | any step | Runs step only when env var parses as true. |

`llm` and `apply` are promptable step types: at least one of `prompt`, `prompt_file`, or `fallback_prompt_id` must be set.

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
