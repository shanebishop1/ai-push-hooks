# ai-push-hooks

AI-assisted pre-push workflow runner for modular repo checks, docs sync, Beads alignment, and PR creation.

## Install

### Python / uv

```bash
uv tool install ai-push-hooks
# or
pipx install ai-push-hooks
```

### npm

```bash
npm install --save-dev ai-push-hooks
# or
pnpm add -D ai-push-hooks
```

The npm binary wraps the bundled Python module, so `python3` (or `python`) must be available.

## Maintainer Release

This repo supports automated dual publishing to PyPI and npm from a git tag.

1. Bump `version` in `pyproject.toml` and `package.json` to the same value.
2. Commit and tag: `git tag vX.Y.Z`.
3. Push commit + tag: `git push && git push --tags`.

The GitHub Actions release workflow then:

- verifies the tag matches both package versions
- runs tests
- builds and validates Python distributions
- smoke-tests the installed Python CLI
- publishes to PyPI (Trusted Publishing)
- publishes to npm (`NPM_TOKEN` secret)

Required one-time setup:

- Configure PyPI Trusted Publisher for this repository.
- Add repository secret `NPM_TOKEN` with publish access to `ai-push-hooks`.

## Commands

```bash
ai-push-hooks hook <remote-name> <remote-url>
ai-push-hooks init --template minimal-docs
```

`init` supports exactly one template: `minimal-docs`. Use `--force` to overwrite an existing config.

## Lefthook Usage

```yaml
pre-push:
  commands:
    ai-push-hooks:
      run: ai-push-hooks hook {1} {2}
```

For local source checkout usage, `./run.sh` works as a wrapper entrypoint.

## Configuration

Put `.ai-push-hooks.toml` in the target repo root. If no file is present, built-in modular defaults are used.

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

The sample below is runnable as-is because each `prompt_file` step also declares a built-in `fallback_prompt_id`. If you add local prompt files, they override the built-ins.

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
prompt_file = ".ai-push-hooks.prompts/beads-status.txt"
fallback_prompt_id = "beads-plan-basic"
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
prompt_file = ".ai-push-hooks.prompts/query.txt"
fallback_prompt_id = "docs-query-basic"
inputs = ["collect/push.diff", "collect/changed-files.txt"]
output = "queries.json"
schema = "string_array"

[[modules.docs.steps]]
id = "analyze"
type = "llm"
prompt_file = ".ai-push-hooks.prompts/analysis.txt"
fallback_prompt_id = "docs-analysis-basic"
inputs = ["collect/push.diff", "collect/docs-context.txt", "query/queries.json", "collect/recent-commits.txt"]
output = "issues.json"
schema = "docs_issue_array"

[[modules.docs.steps]]
id = "apply"
type = "apply"
prompt_file = ".ai-push-hooks.prompts/apply.txt"
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
prompt_file = ".ai-push-hooks.prompts/create-pr.txt"
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

## Layout

- `src/ai_push_hooks/cli.py` - CLI entrypoint
- `src/ai_push_hooks/config.py` - config loading and validation
- `src/ai_push_hooks/engine.py` - scheduler and workflow runtime
- `src/ai_push_hooks/artifacts.py` - run-directory artifact store
- `src/ai_push_hooks/prompts_builtin.py` - built-in fallback prompts
- `src/ai_push_hooks/modules/` - docs, beads, and PR collectors
- `src/ai_push_hooks/executors/` - LLM, apply, exec, and assertion handlers
- `run.sh` - source checkout wrapper
- `bin/ai-push-hooks.js` - npm bin wrapper
- `.ai-push-hooks.toml` - sample config
