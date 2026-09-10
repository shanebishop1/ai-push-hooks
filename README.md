# ai-push-hooks

**Modular AI checks before `git push`.**

Use **OpenCode, Codex, or Claude Code** to review outgoing changes, check that code and docs agree, and apply scoped fixes. Combine AI reasoning with your own scripts, tests, and rules in a repo-local Git hook.

- **Check alignment:** compare changes with documentation, requirements, or task state.
- **Ask or apply:** get findings without edits, or let AI update explicitly allowed files.
- **Verify before pushing:** run deterministic checks and block when your rules fail.
- **Build your workflow:** choose modules, prompts, and runners per step.

## Quick Start

```bash
npm install --save-dev ai-push-hooks@beta
npx --no-install ai-push-hooks init --template minimal-docs
npx --no-install ai-push-hooks install
```

Choose your [runner and model](#choose-your-ai) in `ai-push-hooks.toml`, then push normally. The starter checks docs, applies fixes, and stops for review if anything changed.

[Other installation options](docs/configuration.md#installation) | [Existing hook managers](docs/configuration.md#hook-managers)

## Modular By Design

A workflow is a list of modules. Each module combines the steps it needs:

| Step | Purpose |
| --- | --- |
| `collect` | Gather the outgoing diff and relevant context. |
| `ask` | Ask AI for plain text or schema-validated JSON. |
| `apply` | Let AI edit a temporary workspace; copy back only allowlisted changes. |
| `exec` | Run a command, Python callback, or built-in action. |
| `assert` | Enforce a rule and block the push if it fails. |

Use a review-only module, an apply-only module, or a full collect/ask/apply/verify flow. Add your existing lint and test commands alongside AI checks. No AI runner is needed for deterministic-only workflows.

## Choose Your AI

Set the default runner in `ai-push-hooks.toml`. Change it to `codex` or `claude` to switch tools:

```toml
[llm]
runner = "opencode"

[runners.opencode]
type = "opencode"
model = "provider/model-id" # Replace with an available OpenCode model.
project_access = "artifacts"

[runners.codex]
type = "codex"

[runners.claude]
type = "claude"
```

Each `ask` or `apply` step can override the default with, for example, `runner = "claude"`. You can review with one tool and apply with another.

OpenCode defaults to collected artifacts only. Set `project_access = "project"` for repository reads; Codex and Claude default to project access. Each runner uses its own authentication and model identifiers.

Other tools, including Pi, can use a [custom command runner](docs/configuration.md#custom-runners).

## Example: Docs Alignment

This workflow asks AI to find documentation drift, applies narrow fixes, and requires human review when files change. Use it with the runner settings above:

```toml
[workflow]
modules = ["docs"]

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"

[[modules.docs.steps]]
id = "review"
type = "ask"
prompt = "Check that the docs match the outgoing code changes. Return a JSON array of factual issues with file and description fields, or [] if aligned."
inputs = ["collect/push.diff", "collect/docs-context.txt"]
output = "issues.json"
schema = "docs_issue_array"

[[modules.docs.steps]]
id = "fix"
type = "apply"
prompt = "Fix only the reported documentation drift. Keep edits minimal."
inputs = ["collect/push.diff", "review/issues.json"]
allow_paths = ["README.md", "docs/**/*.md"]

[[modules.docs.steps]]
id = "review-required"
type = "assert"
assertion = "docs_apply_requires_manual_commit"
inputs = ["fix/result.json"]
```

**Want findings only?** Remove the `fix` and `review-required` steps. `ask` saves a response; findings do not block a push unless you add an assertion to evaluate them.

**Want test verification too?** Change the workflow list to `modules = ["docs", "verify"]` and append a module using your project's test command:

```toml
[[modules.verify.steps]]
id = "tests"
type = "exec"
command = ["npm", "test"]
timeout_seconds = 300
```

A nonzero test exit blocks the push. For requirements or plan alignment, use project access and a prompt such as: "Compare the outgoing diff with docs/requirements.md. Identify unmet acceptance criteria and missing tests." AI review complements verification; it does not replace running the tests.

## Extend It

- **Prompts:** write them inline, load a `prompt_file`, or use a built-in prompt.
- **Custom checks:** use argv commands or repo-local Python callbacks.
- **Task alignment:** use the Beads collector and actions with the optional `bd` CLI.
- **Pull requests:** compose a PR with AI and create it with the optional `gh` CLI.
- **Opt-in steps:** gate a step with `when_env`.

See the [configuration guide](docs/configuration.md) for settings, callbacks, and built-in handlers.

## Control And Safety

- Errors block pushes by default. Review and commit any applied edits before retrying.
- `apply` checks file and Git state before and after copying allowlisted changes back. It does not auto-commit.
- Runners, scripts, and callbacks are local programs, not an OS sandbox. Use only trusted configuration.
- Repository content may be sent to your runner's model provider. Review its privacy and billing terms.

Logs and run summaries live under `.git/ai-push-hooks/`. OpenCode transcripts are captured there by default. See [Security](SECURITY.md) for access and data-handling details.

To intentionally skip one push: `AI_PUSH_HOOKS_SKIP=1 git push`.

[Configuration](docs/configuration.md) | [Contributing](CONTRIBUTING.md) | [Changelog](CHANGELOG.md) | [MIT License](LICENSE)
