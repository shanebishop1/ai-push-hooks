# Configuration

Define workflows in `ai-push-hooks.toml` at the repository root. Start with the [README example](../README.md#example-check-your-rules) or the [starter configuration](../ai-push-hooks.toml).

## Installation

The [npm quick start](../README.md#quick-start) installs a repository-local wrapper. It needs Node 18+ and Python 3.10-3.13 on the hook's `PATH`; the npm package does not bundle Python.

For pnpm:

```bash
pnpm add -D ai-push-hooks@beta
pnpm exec ai-push-hooks init --template minimal-docs
pnpm exec ai-push-hooks install
```

For a Python-only installation:

```bash
python -m pip install ai-push-hooks
ai-push-hooks init --template minimal-docs
ai-push-hooks install
```

`uv tool install ai-push-hooks` or `pipx install ai-push-hooks` can replace the pip command. npm uses the `beta` tag while the package is in beta.

AI steps need the selected CLI installed and authenticated with an available model. Deterministic-only workflows do not need one. `init` and `install` refuse to overwrite existing files; `--force` explicitly replaces them.

## Modules And Steps

`[workflow].modules` selects the modules to run. Define each module under `modules.<name>` with a non-empty list of steps. Modules are enabled by default; set `enabled = false` to disable one.

Step inputs refer to earlier artifacts in the same module: `collect/push.diff`, for example. Cross-module inputs are not supported. Independent `collect` and `ask` work may run concurrently; built-in `exec`, `assert`, and `apply` steps are serialized. Trusted custom command runners, including project-access `ask` commands, are not enforced read-only.

| Field | Used by | Meaning |
| --- | --- | --- |
| `id` | All | Unique step name within the module. |
| `type` | All | `collect`, `ask`, `apply`, `exec`, or `assert`. |
| `inputs` | Non-collector steps | Earlier module-local artifacts. |
| `when_env` | All | Run only when the named environment variable is true. |
| `collector` | `collect` | Built-in collector name. |
| `prompt` | `ask`, `apply` | Inline instructions. |
| `prompt_file` | `ask`, `apply` | Repository-relative prompt file. |
| `fallback_prompt_id` | `ask`, `apply` | Built-in prompt name. |
| `runner` | `ask`, `apply` | Override the default runner profile. |
| `output` | `ask` | Required response artifact filename. |
| `schema` | `ask` | Optional JSON schema name. Omit for plain text. |
| `allow_paths` | `apply` | Required list of permitted edit globs. |
| `executor` | `exec` | Built-in action name. |
| `assertion` | `assert` | Built-in assertion name. |
| `python` | `collect`, `exec`, `assert` | Repository-local callback reference. |
| `options` | Python steps | JSON-compatible callback options. |
| `command` | `exec`, `assert` | Direct argv command. |
| `stdin` | Command steps | One declared input to send on stdin. |
| `timeout_seconds` | Command steps | Positive timeout; default `60`. |

Choose one handler per deterministic step: a built-in, a callback, or a command. Prompt resolution uses `prompt`, then `prompt_file`, then `fallback_prompt_id`.

`ask` produces a response, not an automatic policy verdict. Use `assert` to evaluate findings when they should block. `apply` can run without a preceding `ask`; an input ending in `issues.json` containing `[]` skips the apply step.

Upgrading from 0.2.1? Rename `type = "llm"` steps to `type = "ask"`.

## Runner Profiles

`[llm].runner` selects the default; a step's `runner` overrides it. Referenced profiles must exist under `[runners.<name>]`, except for the implicit OpenCode default.

| Profile field | Values / behavior |
| --- | --- |
| `type` | Required: `opencode`, `codex`, `claude`, or `command`. |
| `model` | Runner-specific model identifier. |
| `project_access` | `artifacts` or `project`; OpenCode defaults to `artifacts`, others to `project`. |
| `variant` | Optional OpenCode variant. |
| `command` | Required argv array for command runners. |
| `prompt_transport` | Command runners: `stdin` (default) or `argv`. |

Without an explicit OpenCode profile, `[llm].model` and `[llm].variant` configure it. Explicit profiles use their own model settings. `AI_PUSH_HOOKS_MODEL` overrides the selected profile's model.

Artifact mode supplies collected inputs in a scratch directory. Project mode lets analysis inspect the checkout. Apply always uses a temporary staging copy, with propagation limited by `allow_paths`.

OpenCode runs with isolated configuration and permissions; project/global configuration, external plugins, and MCP servers are not loaded. Codex uses read-only analysis and workspace-write apply modes. Claude Code uses separate analysis and editing permissions. Authenticate the CLI before using it in a hook.

Apply requires a single pushed branch whose local commit is the checked-out `HEAD`. Staging excludes Git metadata, `AGENTS.md`, ignored files, symlinks, and special files. These controls are not an OS sandbox or an automatic rollback system. See [Security](../SECURITY.md).

### Apply and manual commits

`apply` is generic: it can edit any eligible checkout file matching `allow_paths`; it is not limited to Markdown. The runner edits a temporary staging copy, and only validated changes propagate back to the checkout. Those edits do not enter the commit already being pushed, and `apply` never creates a Git commit.

The existing `docs_apply_requires_manual_commit` assertion is a workflow gate, not human-review enforcement. It prevents the original push from passing after `apply` changes files; it cannot prove that anyone reviewed the edits or that they conform to policy. Add it after the `apply` step when that gate is desired:

```toml
[[modules.docs.steps]]
id = "manual-commit"
type = "assert"
assertion = "docs_apply_requires_manual_commit"
inputs = ["apply/result.json"]
```

The assertion checks `apply/result.json`'s `changed_files` and intentionally blocks when edits were propagated. Review `git diff`, run the relevant checks, commit the approved changes, and retry the push. On the retry, the assertion passes when the apply step reports no changes.

## Custom Runners

Use a command profile for another CLI, including Pi, or your own wrapper. This example expects a local `scripts/review-agent` program that reads the prompt from stdin and writes its final response to stdout:

```toml
[runners.custom]
type = "command"
command = ["/absolute/path/to/scripts/review-agent"]
prompt_transport = "stdin"
project_access = "project"
```

Select it with `runner = "custom"` on an `ask` or `apply` step. The runner receives the full instruction and artifact packet. For apply, its working directory is the staging copy.

Commands are argv arrays, not shell strings. Whole-argument placeholders are `{model}`, `{cwd}`, `{stage}`, and `{prompt}`. The `argv` transport requires exactly one `{prompt}` argument; stdin avoids exposing prompts in process listings. Custom programs inherit the user environment and own their permissions and session lifecycle.

## Commands And Callbacks

Command steps run in the real repository, with no implicit shell. Exit zero means success; a nonzero exit blocks the workflow. stdout, stderr, and a `result.json` report are saved as step artifacts.

```toml
[[modules.verify.steps]]
id = "tests"
type = "exec"
command = ["{python}", "-m", "pytest", "-q"]
timeout_seconds = 300
```

Available whole-argument placeholders are `{repo}`, `{python}`, and `{input:<step/artifact>}`. Input placeholders must also appear in `inputs`. Stdin is closed unless `stdin` names a declared input. Command output is bounded to 16 MiB per stream.

For custom context or policy, reference a top-level synchronous Python function:

```toml
[[modules.policy.steps]]
id = "change-size"
type = "assert"
python = "checks/hooks.py:check_size"
options = { max_files = 25 }
```

In `checks/hooks.py`:

```python
from ai_push_hooks.plugins import PluginContext


def check_size(context: PluginContext) -> dict:
    ok = len(context.push.changed_files) <= context.options["max_files"]
    return {"ok": ok, "message": "Change exceeds the configured file limit." if not ok else ""}
```

Add `policy` to `[workflow].modules` to enable it. Callbacks receive repository and step identifiers, push facts, input paths, options, prior module metadata, and a logger.

| Callback type | Return value |
| --- | --- |
| `collect` | `CollectorResult` from `ai_push_hooks.plugins`, with artifacts and metadata. |
| `exec` | JSON-serializable dictionary, saved as `result.json`. |
| `assert` | Dictionary with boolean `ok` and optional string `message`; false blocks. |

Callbacks run as trusted in-process code with already-installed dependencies. There is no callback timeout or automatic dependency installation. Collectors may run concurrently, so custom collectors must be concurrency-safe.

## Built-In Handlers

| Kind | Name | Purpose |
| --- | --- | --- |
| Collector | `docs_context` | Push diff, changed files, docs excerpts, recent commits. |
| Collector | `beads_status_context` | Branch and Beads task context. |
| Collector | `pr_context` | Context for a pull request. |
| Action | `beads_alignment` | Apply supported `bd update` / `bd close` operations. |
| Action | `gh_pr_create` | Create or reuse a PR through `gh`. |
| Assertion | `docs_apply_requires_manual_commit` | Block after applied docs changes. |
| Assertion | `beads_alignment_clean` | Block on unresolved task alignment. |

Beads steps need the native `bd` CLI; PR creation needs `gh`. Gate optional actions with `when_env = "AI_PUSH_HOOKS_CREATE_PR"`, for example. Beads schema and database maintenance remain separate from hook execution.

| JSON schema | Payload |
| --- | --- |
| `string_array` | Array of strings. |
| `docs_issue_array` | Array of objects with `file` and `description`. |
| `beads_alignment_result` | Object with optional `commands` string array. |
| `pr_create_payload` | Object containing PR creation fields. |

Built-in prompts: `docs-query-basic`, `docs-analysis-basic`, `docs-apply-basic`, `beads-plan-basic`, and `pr-compose-basic`.

## Settings

These optional settings supplement the workflow and runner definitions:

| Section | Key | Default |
| --- | --- | --- |
| `general` | `enabled` | `true` |
| `general` | `allow_push_on_error` | `false` |
| `general` | `require_clean_worktree` | `false` |
| `general` | `skip_on_sync_branch` | `true` |
| `general` | `base_branch` | `"main"` |
| `llm` | `runner` | `"opencode"` |
| `llm` | `model` | `"openai/gpt-5.6-luna"` (implicit OpenCode profile) |
| `llm` | `variant` | `""` |
| `llm` | `timeout_seconds` | `800` |
| `llm` | `max_parallel` | `2` |
| `llm` | `json_max_retries` | `2` |
| `llm` | `invalid_json_feedback_max_chars` | `6000` |
| `llm` | `json_retry_new_session` | `true` |
| `llm` | `delete_session_after_run` | `true` |
| `llm` | `max_diff_bytes` | `180000` |
| `llm` | `session_title_prefix` | `"ai-push-hooks"` |
| `logging` | `level` | `"status"` (`info` and `debug` also available) |
| `logging` | `jsonl` | `true` |
| `logging` | `capture_llm_transcript` | `true` (OpenCode only) |
| `logging` | `print_llm_output` | `false` |
| `logging` | `dir` | `".git/ai-push-hooks/logs"` |
| `logging` | `transcript_dir` | `".git/ai-push-hooks/transcripts"` |
| `logging` | `summary_dir` | `".git/ai-push-hooks/summaries"` |

Model availability depends on the provider. Select one available to your account rather than assuming the starter's model is accessible.

## Environment Overrides

| Variable | Effect |
| --- | --- |
| `AI_PUSH_HOOKS_SKIP=1` | Skip the hook before loading configuration. |
| `AI_PUSH_HOOKS_ALLOW_PUSH_ON_ERROR=1` | Log errors but allow the push. |
| `AI_PUSH_HOOKS_REQUIRE_CLEAN=1` | Require a clean worktree. |
| `AI_PUSH_HOOKS_ALLOW_DIRTY=1` | Allow a dirty worktree. |
| `AI_PUSH_HOOKS_BASE_BRANCH` | Override the base branch. |
| `AI_PUSH_HOOKS_MODEL` | Override the selected runner's model. |
| `AI_PUSH_HOOKS_VARIANT` | Override the OpenCode variant. |
| `AI_PUSH_HOOKS_TIMEOUT_SECONDS` | Override the runner timeout. |
| `AI_PUSH_HOOKS_LOG_LEVEL` | Set console verbosity. |
| `AI_PUSH_HOOKS_PRINT_LLM_OUTPUT=1` | Print normalized, redacted final responses; sensitive opt-in. |

Boolean values accept `1/0`, `true/false`, `yes/no`, `y/n`, and `on/off`. Skips and fail-open settings are explicit policy choices, not successful checks.

## Hook Managers

The built-in `install` command creates a repository-local pre-push hook without changing Git configuration. Existing hooks require explicit `--force` replacement; shared/external paths and symlinks are refused. Keep the installed Python executable or local npm package available at its installed location.

For Lefthook, use this in `lefthook.yml` instead of the built-in installer:

```yaml
pre-push:
  commands:
    ai-checks:
      run: npx --no-install ai-push-hooks hook {1} {2}
      use_stdin: true
```

Run `lefthook install`. For a Python installation, omit the `npx --no-install` prefix. Other hook managers must likewise forward Git's remote arguments, ref-update stdin, and exit status. If earlier commands consume stdin, capture it first and replay it to ai-push-hooks.

To remove the generated hook, inspect `git rev-parse --git-path hooks/pre-push` and remove only the ai-push-hooks delegate or its entry in your hook manager. Preserve unrelated hook commands.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Hook does not run | Reinstall with your chosen hook manager and inspect `git config --get core.hooksPath`. |
| Missing runner or authentication failure | Ensure the CLI is on the hook's `PATH`, authenticated, and configured with an available model. |
| Unknown profile | Match `runner` to an existing `[runners.<name>]`. |
| Runner capability error | Update the CLI to a version with the required adapter flags. |
| Invalid JSON | Check the prompt and schema; invalid JSON is retried twice by default. |
| Push blocked after edits | Review `git diff`, run checks, commit approved changes, and retry the push. |
| Need diagnostics | Inspect `.git/ai-push-hooks/logs` and `.git/ai-push-hooks/summaries`. |

For validation commands, see [Contributing](../CONTRIBUTING.md).
