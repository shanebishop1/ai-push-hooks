# ai-push-hooks

**Agentic linting for the rules your coding agent forgot.**

`AGENTS.md` tells an agent how to work. **ai-push-hooks checks whether it followed through.** Think of it as the inverse of `AGENTS.md`: a second pass over outgoing changes before `git push`, catching guidelines the agent forgot or neglected.

Use **OpenCode, Codex, or Claude Code** to check rules that need judgment, not just a regex. Report violations, apply scoped fixes, and block pushes with explicit checks. It's a hedge against missed instructions, not a guarantee that AI catches everything.

## Rules Worth Checking

| Your rule | What the check looks for |
| --- | --- |
| No empty marketing speak. | Vague claims and filler in changed pages. |
| Use our typed API client. | Components making direct backend requests. |
| Test behavior, including failures. | Changed behavior without meaningful test coverage. |
| Keep migrations backward-compatible. | Destructive changes that break a rolling deployment. |

Write your own rules in prompts or supply a rules file as context. These are examples of checks you configure, not built-in guarantees.

## Why Now?

With lower-cost models such as GPT 5.6 Luna, GLM 5.3 Flash, Muse Spark 1.3, and Gemini Flash, running several focused agentic checks on each push can be practical, rather than reserving AI review for special occasions. Keep context narrow and measure your workflow's cost and latency. GPT 5.6 Luna is the default.

## Quick Start

Install the [ai-push-hooks skill](skills/ai-push-hooks/SKILL.md), tell your agent what intelligent checks you want before pushes, and let it set up the modules for you.

```bash
npm install --save-dev ai-push-hooks@beta
npx --no-install ai-push-hooks init
npx --no-install ai-push-hooks install
```

Install and authenticate your chosen AI CLI. The current `init` starter checks documentation; replace its configuration with the rules-checking example below to check `AGENTS.md` instead. Then push normally.

[Other installation options](docs/configuration.md#installation) | [Existing hook managers](docs/configuration.md#hook-managers)

## Example: Check Your Rules

This workflow supplies the outgoing diff and your root `AGENTS.md` explicitly, asks for violations, and blocks if any are reported. It does not rely on the runner automatically loading agent instructions.

In `checks/hooks.py`:

```python
import json

from ai_push_hooks.plugins import CollectorResult, PluginContext


def collect_rules(context: PluginContext) -> CollectorResult:
    return CollectorResult(artifacts={
        "push.diff": context.push.diff_text,
        "rules.txt": (context.repo_root / "AGENTS.md").read_text(encoding="utf-8"),
    })


def assert_rules(context: PluginContext) -> dict:
    issues = json.loads(context.inputs["review/issues.json"].read_text(encoding="utf-8"))
    return {"ok": issues == [], "message": "Review findings in review/issues.json."}
```

In `ai-push-hooks.toml`:

```toml
[llm]
runner = "opencode"

[runners.opencode]
type = "opencode"
model = "openai/gpt-5.6-luna"
project_access = "project"

[workflow]
modules = ["rules"]

[[modules.rules.steps]]
id = "collect"
type = "collect"
python = "checks/hooks.py:collect_rules"

[[modules.rules.steps]]
id = "review"
type = "ask"
prompt = "Check the outgoing diff against rules.txt. Inspect repository context as needed. Report only concrete violations introduced by these changes, with file and description fields. Return a JSON array, or [] if none. Do not edit files."
inputs = ["collect/push.diff", "collect/rules.txt"]
output = "issues.json"
schema = "docs_issue_array"

[[modules.rules.steps]]
id = "gate"
type = "assert"
python = "checks/hooks.py:assert_rules"
inputs = ["review/issues.json"]
```

`docs_issue_array` is the existing schema name for `{file, description}` findings; it works for code rules too. A missing rules file or a failed check blocks the push by default. Findings live in the run artifacts under `.git/ai-push-hooks/`.

**Want fixes too?** Add an `apply` step with the findings, your rules, and an explicit `allow_paths` list, then recheck and run tests. Applied edits are not auto-committed: review and commit them before retrying the push.

## Build Your Workflow

Each module combines the steps it needs:

| Step | Purpose |
| --- | --- |
| `collect` | Gather the outgoing diff and relevant context. |
| `ask` | Ask AI for findings or another response. |
| `apply` | Edit a temporary workspace; propagate only allowlisted changes. |
| `exec` | Run scripts, tests, or other actions. |
| `assert` | Evaluate a verdict and block the push if it fails. |

`ask` alone does not block on findings; add an `assert` gate. Combine AI review with your existing linters and tests, not instead of them. Independent analysis can run concurrently; built-in `exec`, `assert`, and `apply` steps are serialized. Trusted custom command runners, including project-access `ask` commands, are not enforced read-only.

## Choose Your AI

OpenCode defaults to **`openai/gpt-5.6-luna`**. Explicit runner profiles use their own model settings. To use Codex or Claude Code, add its profile and change `[llm].runner`:

```toml
[runners.codex]
type = "codex"

[runners.claude]
type = "claude"
```

Each `ask` or `apply` step can override the runner, so one tool can review and another can fix. Use model identifiers available to your provider. Other tools, including Pi, can use a [custom command runner](docs/configuration.md#custom-runners).

## Control And Safety

- Errors block pushes by default. AI judgments can still miss violations or report false positives.
- `apply` validates file and Git state and limits propagated edits to `allow_paths`. It does not auto-commit.
- Runners, scripts, and callbacks are trusted local programs, not an OS sandbox.
- Repository content may be sent to your model provider. Review its privacy and billing terms.

Logs and run summaries live under `.git/ai-push-hooks/`. OpenCode transcripts are captured there by default. To intentionally skip one push: `AI_PUSH_HOOKS_SKIP=1 git push`.

[Configuration](docs/configuration.md) | [Security](SECURITY.md) | [Contributing](CONTRIBUTING.md) | [Changelog](CHANGELOG.md) | [MIT License](LICENSE)
