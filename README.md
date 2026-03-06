# ai-doc-sync-hook

AI-assisted pre-push workflow runner for docs sync, Beads alignment, and PR creation.

## Install

### Python / uv

```bash
uv tool install ai-doc-sync-hook
# or
pipx install ai-doc-sync-hook
```

### npm

```bash
npm install --save-dev ai-doc-sync-hook
# or
pnpm add -D ai-doc-sync-hook
```

The npm binary wraps the bundled Python module, so `python3` (or `python`) must be available.

## Commands

```bash
ai-doc-sync-hook hook <remote-name> <remote-url>
ai-doc-sync-hook init --template minimal-docs
```

`init` supports exactly one template: `minimal-docs`. Use `--force` to overwrite an existing config.

## Lefthook Usage

```yaml
pre-push:
  commands:
    ai-doc-sync:
      run: ai-doc-sync-hook hook {1} {2}
```

For local source checkout usage, `./run.sh` works as a wrapper entrypoint.

## Configuration

Put `.ai-doc-sync.toml` in the target repo root. If no file is present, built-in modular defaults are used.

Prompt resolution precedence is:

1. inline `prompt`
2. `prompt_file`
3. built-in `fallback_prompt_id`

Minimal docs example:

```toml
[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"

[[modules.docs.steps]]
id = "query"
type = "llm"
prompt = "Return a JSON array of documentation search queries. JSON only."
inputs = ["collect/push.diff", "collect/changed-files.txt"]
output = "queries.json"
schema = "string_array"

[[modules.docs.steps]]
id = "analyze"
type = "llm"
prompt = "Return JSON issues only for factual documentation drift."
inputs = ["collect/push.diff", "collect/docs-context.txt", "query/queries.json", "collect/recent-commits.txt"]
output = "issues.json"
schema = "docs_issue_array"

[[modules.docs.steps]]
id = "apply"
type = "apply"
prompt = "Apply the minimum Markdown fixes required."
inputs = ["collect/push.diff", "collect/docs-context.txt", "analyze/issues.json"]
allow_paths = ["README.md", "docs/**/*.md"]

[[modules.docs.steps]]
id = "assert"
type = "assert"
assertion = "docs_apply_requires_manual_commit"
inputs = ["apply/result.json"]
```

Example config that recreates the current docs + beads + PR behavior through configuration only:

```toml
[workflow]
modules = ["beads", "docs", "pr"]

[modules.beads]
enabled = true

[[modules.beads.steps]]
id = "collect"
type = "collect"
collector = "beads_status_context"

[[modules.beads.steps]]
id = "plan"
type = "llm"
prompt_file = ".ai-doc-sync.prompts/beads-status.txt"
inputs = ["collect/branch-context.txt", "collect/changed-files.txt", "collect/push.diff", "collect/commits.txt"]
output = "beads-plan.json"
schema = "beads_alignment_result"

[[modules.beads.steps]]
id = "apply"
type = "exec"
executor = "beads_alignment"
inputs = ["plan/beads-plan.json"]

[[modules.beads.steps]]
id = "assert"
type = "assert"
assertion = "beads_alignment_clean"
inputs = ["plan/beads-plan.json"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"

[[modules.docs.steps]]
id = "query"
type = "llm"
prompt_file = ".ai-doc-sync.prompts/query.txt"
inputs = ["collect/push.diff", "collect/changed-files.txt"]
output = "queries.json"
schema = "string_array"

[[modules.docs.steps]]
id = "analyze"
type = "llm"
prompt_file = ".ai-doc-sync.prompts/analysis.txt"
inputs = ["collect/push.diff", "collect/docs-context.txt", "query/queries.json", "collect/recent-commits.txt"]
output = "issues.json"
schema = "docs_issue_array"

[[modules.docs.steps]]
id = "apply"
type = "apply"
prompt_file = ".ai-doc-sync.prompts/apply.txt"
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
prompt_file = ".ai-doc-sync.prompts/create-pr.txt"
inputs = ["collect/pr-context.txt", "collect/changed-files.txt", "collect/push.diff", "collect/commits.txt"]
output = "pr-draft.json"
schema = "pr_create_payload"

[[modules.pr.steps]]
id = "create"
type = "exec"
executor = "gh_pr_create"
when_env = "AI_DOC_SYNC_CREATE_PR"
inputs = ["compose/pr-draft.json"]
```

## Layout

- `src/ai_doc_sync_hook/cli.py` - CLI entrypoint
- `src/ai_doc_sync_hook/config.py` - config loading and validation
- `src/ai_doc_sync_hook/engine.py` - scheduler and workflow runtime
- `src/ai_doc_sync_hook/artifacts.py` - run-directory artifact store
- `src/ai_doc_sync_hook/prompts_builtin.py` - built-in fallback prompts
- `src/ai_doc_sync_hook/modules/` - docs, beads, and PR collectors
- `src/ai_doc_sync_hook/executors/` - LLM, apply, exec, and assertion handlers
- `run.sh` - source checkout wrapper
- `bin/ai-doc-sync-hook.js` - npm bin wrapper
- `.ai-doc-sync.toml` - sample config
