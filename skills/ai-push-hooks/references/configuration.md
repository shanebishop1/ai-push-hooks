# Configuration

Root `ai-push-hooks.toml` overrides built-in defaults; environment overrides follow. `[workflow].modules` selects named modules; module `enabled` defaults to `true`. Step-level `runner` overrides `[llm].runner`. See [workflow recipes](workflows.md) for complete checks.

## Runners

```toml
[llm]
runner = "opencode"

[runners.opencode]
type = "opencode"
model = "openai/gpt-5.6-luna"
project_access = "project"

[runners.codex]
type = "codex"

[runners.claude]
type = "claude"
```

Select a profile with `[llm].runner` or `runner = "codex"` on an `ask`/`apply` step. Names must exist except for the implicit OpenCode profile. Choose model IDs supported by the provider/account; the example model is not an availability promise.

| Setting | Contract |
| --- | --- |
| `type` | `opencode`, `codex`, `claude`, or `command`. |
| `model` | Profile-specific model; explicit profiles use their own settings, not `[llm].model`. |
| `variant` | Optional OpenCode variant. |
| `project_access` | `artifacts` supplies collected inputs in scratch space; `project` permits checkout inspection. OpenCode defaults to `artifacts`, other types to `project`. |

Without an explicit OpenCode profile, `[llm].model`/`variant` configure it. `AI_PUSH_HOOKS_MODEL` overrides the selected profile's model. OpenCode isolates configuration and permissions, disabling project/global config, external plugins, and MCP servers. Codex uses read-only analysis and workspace-write apply; Claude uses separate analysis/edit permissions. Apply always uses a staging copy. Supply repository rules as artifacts explicitly.

### Custom CLI

For Pi or another program, define a trusted wrapper that reads the prompt on stdin and writes only its final response to stdout:

```toml
[runners.custom]
type = "command"
command = ["/absolute/path/to/scripts/review-agent"]
prompt_transport = "stdin"
project_access = "project"
```

Select `runner = "custom"`. This is a direct argv array, not a shell string. Whole-argument placeholders: `{prompt}`, `{model}`, `{cwd}`, `{stage}`. `prompt_transport = "argv"` requires exactly one `{prompt}` argument; stdin transport must omit it and avoids exposing prompts in process listings. Custom runners inherit the environment and own permissions/session cleanup; apply runs them in staging.

## Defaults

| Section | Keys And Defaults |
| --- | --- |
| `general` | `enabled = true`, `allow_push_on_error = false`, `require_clean_worktree = false`, `skip_on_sync_branch = true`, `base_branch = "main"`. |
| `llm` | `runner = "opencode"`, `model = "openai/gpt-5.6-luna"` (implicit profile), `variant = ""`, `timeout_seconds = 800`, `max_parallel = 2`. |
| `llm` JSON | `json_max_retries = 2`, `invalid_json_feedback_max_chars = 6000`, `json_retry_new_session = true`. |
| `llm` context/session | `max_diff_bytes = 180000`, `delete_session_after_run = true`, `session_title_prefix = "ai-push-hooks"`. |
| `logging` | `level = "status"` (`info`/`debug` also available), `jsonl = true`, `capture_llm_transcript = true` (OpenCode only), `print_llm_output = false`. |
| `logging` paths | `dir = ".git/ai-push-hooks/logs"`, `transcript_dir = ".git/ai-push-hooks/transcripts"`, `summary_dir = ".git/ai-push-hooks/summaries"`. |

The sync branch defaults to `beads-sync`, or `BEADS_SYNC_BRANCH` when set. Diff truncation means the model may not see every change; keep checks focused and retain deterministic tests.

## Environment Overrides

| Variable | Effect |
| --- | --- |
| `AI_PUSH_HOOKS_SKIP=1` | Bypass before loading config. |
| `AI_PUSH_HOOKS_ALLOW_PUSH_ON_ERROR=1` | Fail open; not proof of successful checks. |
| `AI_PUSH_HOOKS_REQUIRE_CLEAN=1` | Require clean worktree. |
| `AI_PUSH_HOOKS_ALLOW_DIRTY=1` | Allow dirty worktree; wins over require-clean. |
| `AI_PUSH_HOOKS_BASE_BRANCH` | Base branch override. |
| `AI_PUSH_HOOKS_MODEL` / `AI_PUSH_HOOKS_VARIANT` | Selected runner model / OpenCode variant override. |
| `AI_PUSH_HOOKS_TIMEOUT_SECONDS` | Runner timeout override. |
| `AI_PUSH_HOOKS_LOG_LEVEL` | Console verbosity. |
| `AI_PUSH_HOOKS_PRINT_LLM_OUTPUT=1` | Print normalized/redacted final responses; sensitive opt-in. |

Booleans accept `1/0`, `true/false`, `yes/no`, `y/n`, `on/off`. Keep bypass/fail-open controls unset unless explicitly authorized. Prompts, diffs, project files, outputs, and transcripts can contain private data; inspect provider privacy/billing terms and do not publish runtime artifacts. Local session deletion does not establish provider-side deletion.
