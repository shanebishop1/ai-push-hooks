# ai-push-hooks

`ai-push-hooks` catches repository drift before it reaches a remote. It turns `git push` into a configurable workflow that can inspect the exact outgoing diff, ask a selected local runner for structured findings, apply narrowly allowlisted documentation fixes, run deterministic actions, and block the push until changes are reviewed and committed.

Use it to keep docs aligned with code, check branch/task consistency, or prepare pull requests without replacing your project's ordinary lint, test, and build checks. Workflows are assembled from `collect`, `ask`, `apply`, `exec`, and `assert` steps and default to failing closed.

## Quick start: repo-local hook

### Prerequisites

- [Git](https://git-scm.com/downloads) and a POSIX shell for the generated hook.
- [Python 3.10–3.13](https://www.python.org/downloads/). Python is required even when installing the npm wrapper. The wrapper probes Python 3.14, 3.13, 3.12, 3.11, 3.10, then `python`; the 3.14 probe is not a beta support claim. Python 3.10 additionally needs the `tomli` package available to that interpreter.
- A runner CLI is optional for workflows that use only deterministic steps, but the `minimal-docs` starter uses OpenCode for `ask` and `apply`. The selected runner still needs its normal provider/model authentication: for OpenCode, `opencode auth list` is a useful check; Codex, Claude, and custom commands use their own user-managed setup.
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
availability, pricing, and authentication are provider-dependent. Model strings
are opaque runner-specific identifiers; ai-push-hooks does not maintain a model
allowlist. To use a free OpenCode Zen model, run `opencode models opencode`,
choose a model currently marked free, and set `[llm].model` to that full
identifier. Free-model availability changes over time, so do not treat the model
used in the recorded preview below as a permanent recommendation or default.

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

### Version boundary

**Published `0.2.1`.** The published beta is the compatibility baseline. Its
model-backed workflow spelling was historically `type = "llm"`, and it does not
promise the source-tree runner profiles or pluggable workflow steps described
below.

**Source-unreleased in this checkout.** The current source uses `type = "ask"`
(not `llm` or `agent`), adds repository-local Python callbacks and direct
`exec`/`assert` commands, and supports named runner profiles. These examples
are copyable source-tree configuration, not a claim that the published
`0.2.1` wheel or npm package already contains them. There is no compatibility
alias: update the configuration when consuming a release that publishes this
rename.

**Deferred/proposed.** Automatic discovery, installed-module references,
package-relative hook loading, a plugin SDK, remote services, sandboxing, and a
universal external-agent observer remain out of scope. Architecture-doc
changes are deferred; this README documents usage and trust boundaries only.

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

### Runner profiles and access modes

> **Unreleased source note:** The selectable runner profiles described here are
> source-tree behavior and are not included in the published `0.2.1` wheel or
> npm artifacts. Do not assume `pip install ai-push-hooks==0.2.1` or
> `ai-push-hooks@beta` provides this feature until a release explicitly includes
> it.

`ask` and `apply` steps select a strict named profile. `[llm].runner` is the
workflow default; a `runner` on an individual `ask` or `apply` step overrides it.
`runner` is rejected on `collect`, `exec`, and `assert`. Every referenced name
must exist under `[runners.<name>]`, except `opencode`, which has an implicit
compatibility profile. Unknown profile fields, missing names, invalid
placeholders, and type-inapplicable fields fail during config loading rather
than being ignored.

The four static profile types are `opencode`, `codex`, `claude`, and `command`.
`model` is an opaque identifier passed to the selected runner. A profile's
`project_access` is either `artifacts` or `project`; it defaults to `artifacts`
for OpenCode and `project` for the other three types. The existing flat
`[llm]` form remains the shipped default:

```toml
[llm]
runner = "opencode"
model = "openai/gpt-5.6-terra"
variant = ""
```

It is equivalent to an OpenCode profile with `project_access = "artifacts"`.
That means read-only analysis uses an empty scratch directory with only
validated hook artifacts attached, with OpenCode tools denied, and compatibility
apply uses a private staging projection limited to eligible files matching its
allowlist. It does **not** silently become project-aware. In explicit OpenCode
project mode, read/list/glob/grep can inspect the real checkout for analysis;
edits, shell, tasks, web, sharing, plugins, MCP, and project/global config remain
restricted.
Opt into OpenCode project reads explicitly:

```toml
[llm]
runner = "opencode-project"

[runners.opencode-project]
type = "opencode"
model = "openai/gpt-5.6-terra"
project_access = "project"
```

The other built-in examples are:

```toml
[llm]
runner = "codex-review"

[runners.codex-review]
type = "codex"
model = "gpt-5.6-codex"
project_access = "project"

[runners.claude-review]
type = "claude"
model = "sonnet"
project_access = "project"

[[modules.docs.steps]]
id = "analyze"
type = "ask"
runner = "claude-review" # per-step override
fallback_prompt_id = "docs-analysis-basic"
inputs = ["collect/push.diff"]
output = "issues.json"
schema = "docs_issue_array"
```

The generic command profile is the supported way to describe a Pi invocation;
Pi is not a fourth built-in adapter:

```toml
[runners.pi-apply]
type = "command"
model = "provider/exact-model-id"
project_access = "project"
prompt_transport = "stdin"
command = [
  "pi", "--print", "--no-session", "--no-extensions", "--no-skills",
  "--no-prompt-templates", "--no-themes", "--no-context-files",
  "--tools", "read,grep,find,ls,edit,write", "--model", "{model}"
]

[[modules.docs.steps]]
id = "apply"
type = "apply"
runner = "pi-apply"
fallback_prompt_id = "docs-apply-basic"
allow_paths = ["README.md", "docs/**/*.md"]
```

Command profiles are argv vectors, never shell command strings. The executable
is launched with no implicit shell, shell expansion, pipes, redirection,
globbing, or command substitution. `stdin` (the default) sends the complete
instruction/artifact packet and then EOF. `argv` requires exactly one whole
argument `{prompt}`. The other whole-argument placeholders are `{model}`,
`{cwd}`, and `{stage}`; substring forms such as `--prompt={prompt}` are
rejected. An argv prompt can be visible in process listings, so stdin is the
safer default. Custom commands inherit the hook environment and have no
ai-push-hooks lifecycle or permission enforcement; stdout is the final result,
and stderr is diagnostic only.

### Project visibility and apply boundary

For `project_access = "project"`, an analysis runner receives the real
repository root as its cwd. For `apply`, it receives a point-in-time temporary
projection, never the real checkout. Project apply copies eligible tracked or
unignored ordinary files, including the current dirty baseline, then allows
propagation only for paths matching that step's `allow_paths`. Ignored files are
excluded even when tracked-but-ignored (the `--no-index` ignore check is
intentional). Git metadata, casefolded/Unicode-normalized `AGENTS.md` paths,
symlinks/reparse points, and special files are excluded or rejected.

Staging and propagation are bounded: at most 10,000 staged entries and 256 MiB
of staging content, with Git metadata snapshots bounded at 20,000 entries and
64 MiB. Runner input artifacts are capped at an aggregate 16 MiB, and each
captured child stdout/stderr stream is capped at 16 MiB. Changes outside the
allowlist, unsafe modes (setuid/setgid/sticky), destination type/content/mode
conflicts, or protected Git-state changes fail closed. Existing baseline checks
and atomic file replacement reduce lost updates; they are not an atomic
compare-and-swap against an arbitrary external writer. No rollback is attempted
over pre-existing user changes.

## Pluggable workflow steps (source-unreleased)

The source tree supports two deliberately small extension seams. A deterministic
step may use one repository-local Python callback, or `exec`/`assert` may use a
direct argv command. This is configuration-defined trusted code, not a plugin
framework or SDK.

### Python callbacks

Reference one explicit file and one top-level synchronous callable:
`checks/hooks.py:collect_context`. The file is a contained, ordinary `.py`
file; symlink/reparse traversal, attribute chains, installed-module references,
and package-relative loading are not supported. The callback is imported only
after the enabled-module gate, `when_env` gate, and input resolution. A source
file is loaded once per run (including concurrent collectors), with no `sys.path`,
cwd, or environment mutation. It may import standard-library or already
installed dependencies from the interpreter running the hook; the host never
runs `pip`. There is no hot reload, isolation sandbox, or enforceable hard
timeout for in-process Python.

Every callback receives exactly one frozen `PluginContext`. Its `repo_root`,
`module_id`, and `step_id` identify the call; `inputs` is an insertion-ordered,
read-only mapping from logical artifact references to validated `Path` values;
`options` and `prior_module_metadata` are recursive read-only snapshots; and
`push` contains bounded push facts (`branch_name`, `checked_out_branch`,
`base_branch`, `ranges`, `changed_files`, `diff_text`, and `push_updates`). The
existing thread-safe `logger` is also available. These values are immutable API
containers, not read-only filesystem handles.

This is a complete callback example for all three deterministic callback kinds:

```python
# checks/hooks.py
import json

from ai_push_hooks.plugins import CollectorResult, PluginContext


def collect_context(context: PluginContext) -> CollectorResult:
    return CollectorResult(
        artifacts={"files.json": list(context.push.changed_files)},
        metadata={"collected_by": context.step_id},
    )


def run_check(context: PluginContext) -> dict:
    files = json.loads(context.inputs["context/files.json"].read_text(encoding="utf-8"))
    return {"file_count": len(files), "severity": context.options["severity"]}


def assert_policy(context: PluginContext) -> dict:
    result = json.loads(context.inputs["check/result.json"].read_text(encoding="utf-8"))
    ok = result["file_count"] <= context.options["max_files"]
    return {"ok": ok, "message": "too many changed files" if not ok else ""}
```

The corresponding step declarations are:

```toml
[[modules.quality.steps]]
id = "context"
type = "collect"
python = "checks/hooks.py:collect_context"
options = { include_generated = false, severity = "high" }

[[modules.quality.steps]]
id = "check"
type = "exec"
python = "checks/hooks.py:run_check"
inputs = ["context/files.json"]
options = { severity = "high" }

[[modules.quality.steps]]
id = "policy"
type = "assert"
python = "checks/hooks.py:assert_policy"
inputs = ["check/result.json"]
options = { max_files = 25 }
```

`collect` callbacks return `CollectorResult` (artifacts, metadata, and optional
module skip state). `exec` callbacks return a JSON-serializable `dict`, saved
as `result.json` without automatically merging into module metadata. `assert`
callbacks return a JSON-serializable `dict` with an actual boolean `ok` and,
when present, a string `message`; the report is saved before `ok = false` blocks.
Artifact names and serialization are validated, each plugin artifact is bounded
to 16 MiB, and the aggregate collect payload is bounded to 64 MiB. Callback
exceptions, import failures, `SystemExit`, coroutine functions, and awaitable
returns fail closed with a concise named error; `KeyboardInterrupt` is not
swallowed. Callback `print()` calls and direct host filesystem writes are
outside host sanitization and remain the author's responsibility.

`collect` callbacks retain the read-only/concurrent scheduling class and may
overlap up to `max_parallel`; callback authors must make them concurrency-safe.
Python `exec` and `assert` steps are serialized with other mutating work.

### Direct argv commands

For `exec` and `assert`, `command` is a non-empty argv array. It runs with
`shell = false`, the repository root as cwd, the inherited user environment,
and stdin closed with EOF by default. `stdin = "<logical-ref>"` instead streams
that exact declared input artifact. The default command timeout is **60 seconds**;
configured values must be positive. There is no command allowlist or `trusted`
flag, and an explicit `bash -c` is the user's choice to adopt shell semantics.

```toml
[[modules.quality.steps]]
id = "lint"
type = "exec"
command = ["{python}", "scripts/lint_changed.py", "{input:context/files.json}"]
inputs = ["context/files.json"]
stdin = "context/files.json"
timeout_seconds = 60

[[modules.quality.steps]]
id = "policy-command"
type = "assert"
command = ["bash", "-c", "test -s \"$1\"", "assert", "{input:lint/result.json}"]
inputs = ["lint/result.json"]
```

Reserved substitution happens only when the entire argv element is one exact
token: `{repo}` is the canonical repository root, `{python}` is the interpreter
running the hook, and `{input:<logical-ref>}` is a validated declared input.
The token grammar is one `{...}` pair containing only letters, digits, `_`, `.`,
`/`, `:`, or `-`; unknown tokens in that grammar (such as `{repos}`) are
rejected, as are embedded recognized tokens such as `--path={repo}`. Other
braces are literal command text, so Bash brace expansion, `awk '{print $1}'`,
and `python -c 'print({"a": 1})'` pass unchanged. Substitution is not shell
parsing or host-side string interpolation.

Both streams are captured as private, unredacted step artifacts named
`stdout.txt` and `stderr.txt`, including empty streams, and are not printed to
the console by default. `result.json` records the return code, artifact
references, and truncation flags. Valid stream text is UTF-8 and preserves
Unicode exactly; invalid UTF-8, a missing executable, timeout, signal, or a
stream exceeding the **16 MiB per-stream** bound is a process error and fails
closed. Captured output is retained when a process started. Exec requires exit
zero (an empty stdout is still success). Assert records `ok = true` for exit
zero; a nonzero exit records `ok = false` plus a bounded redacted message,
saves all reports first, and then blocks. Commands run in the real checkout and
may modify it; this is intentionally different from `apply`'s protected
staging projection. Exec/assert commands are serialized.

Only the workflow-level `general.allow_push_on_error = true` (or its explicit
`AI_PUSH_HOOKS_ALLOW_PUSH_ON_ERROR=1` override) changes a failure into a
fail-open warning. There is no per-command fail-open switch or custom success
exit-code list.

### Ask and apply are separate

`ask` reads/reasons and returns a response. Omitting `schema` deliberately gives
plain text and does not implicitly block a push or perform an action:

```toml
[[modules.review.steps]]
id = "summary"
type = "ask"
runner = "reviewer"
prompt = "Summarize the outgoing change in plain text."
output = "summary.txt"
# No schema: this is a response, not a verdict.
```

`apply` inspects a staging projection and may edit it. A preceding `ask` is not
required, and JSON is not required. This standalone, no-JSON apply flow declares
the workflow, enables its module, selects a runner, and supplies an explicit
allowlist:

```toml
[llm]
runner = "docs-apply"

[runners.docs-apply]
type = "opencode"
model = "openai/gpt-5.6-terra"
project_access = "project"

[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "apply-docs"
type = "apply"
runner = "docs-apply"
prompt = "Inspect the readable project projection and fix only factual drift in the allowed files."
allow_paths = ["README.md", "docs/**/*.md"]
```

The apply projection and propagation checks remain distinct: readable project
files may exceed the propagation allowlist, but only allowlisted changes can
propagate and any other staging change fails. The existing filename-specific
legacy shortcut also remains: an apply input whose filename ends in
`issues.json` containing the empty JSON list skips apply. It is legacy behavior,
not a general condition language or a replacement for an explicit policy step.

### Safeguards versus user policy

| Surface | Host-enforced safeguard | User/trusted-author policy |
| --- | --- | --- |
| Python reference/loading | Contained no-follow regular file; lazy reached-only import; per-run cache | Callback imports, direct writes, prints, and termination behavior |
| Python context/results | Frozen snapshots; exact result types; JSON/artifact bounds; fail-closed malformed results | Semantic correctness and concurrency safety |
| Command | Direct argv, cwd/stdin contract, timeout, bounded capture, UTF-8 validation, private artifacts | Inherited environment, explicit `bash -c`, executable behavior, checkout writes |
| Assert | Strict Python boolean or command exit status; report saved before failure | Business verdict logic and whether a verdict should block |
| Ask | Selected runner/profile, access mode, schema handling, and existing runner controls | Model quality; add `assert` if findings should block |
| Apply | Staging inventory, allowlist propagation, Git/baseline/integrity checks | Runner/tool policy and prompt intent; no OS sandbox or atomic CAS claim |

### OpenCode isolation limits

OpenCode uses `--pure`, isolated home/config/cache/state directories, disabled
sharing, and an ai-push-hooks-owned agent configuration. External plugins,
project/global configuration, MCP servers, instructions, and custom providers
from inherited global configuration are not loaded. Built-in plugins remain
available, including built-in authentication such as Codex OAuth. The existing
OpenCode data directory and recognized provider environment variables (including
`OPENAI_API_KEY`) are retained/forwarded, and OpenCode chooses authentication
using its own normal precedence.

These are permission and temporary-workspace controls, not an operating-system
sandbox. Every runner is a local program with the invoking user's OS identity;
same-user code can access other host paths. There is no mandatory command
allowlist, shell parser, container, credential broker, or trust prompt. Use an
external sandbox, container, VM, or low-privilege account when that boundary is
required. The scheduler may overlap `collect`/`ask` work up to `max_parallel`,
so a trusted custom `ask` command must really be safe for concurrent access;
`apply` remains globally serialized but custom command behavior is not enforced.

### Invocation, lifecycle, and output

All adapters use direct argv execution, explicit cwd, separate stdout/stderr
capture, and the configured per-invocation timeout. The built-in mappings are:

| Profile | Analysis | Apply |
| --- | --- | --- |
| OpenCode | `opencode run --agent ... --pure --format json --model ...` with native attachments | same isolated agent in the selected staging projection |
| Codex | `codex exec --json --color never --sandbox read-only --ephemeral --cd <project> [--model <model>] -` | same with `--sandbox workspace-write` and `--skip-git-repo-check` |
| Claude | `claude -p --output-format json --no-session-persistence [--model <model>]` plus tested read-only flags | same with tested edit/write flags |
| Command/Pi | configured argv, prompt on stdin by default | configured argv in staging; host-side propagation checks still apply |

Codex and Claude require their advertised capability flags; a missing required
flag fails instead of silently weakening the policy. A successful built-in call
must have a successful terminal result and final text. The workflow still owns
JSON extraction, schema validation, bounded feedback, and retries.

After every call, the completion event identifies the module, step, purpose,
profile, adapter type, and success/failure. It reports a returned session ID and
session state when available, plus a local transcript path when one was really
created; unavailable lifecycle data is not invented. OpenCode retains its
existing export-to-private-storage and cleanup behavior: transcript capture is
on by default and `delete_session_after_run` is on by default. To inspect an
exported JSON transcript, use a truthful local view such as:

```bash
python -m json.tool "path/to/exported-transcript.json"
```

An OpenCode session retained by setting `delete_session_after_run = false`
does not cause ai-push-hooks to print a resume command. In particular, ordinary
`opencode -s ...` is not a valid way to resume a session created from an
arbitrary repository/temp-project context; the mock contract test confirms this
case. Codex is ephemeral and Claude disables session persistence, so neither
claims a resumable/provider transcript. A command profile has no lifecycle
inference at all; wrappers own any files or resume behavior they implement.

Console status lines have an `[ai-push-hooks]` prefix. LLM lines include
`module.step` and purpose, and completion lines include profile/type and a
success or failure label. `NO_COLOR` disables colors; otherwise non-empty
`FORCE_COLOR` (except `0`) enables them, while `TERM=dumb` disables them when
not forced. `logging.jsonl = true` writes plain JSONL records to the private
runtime log; console records remain one line. `print_llm_output` is a sensitive,
explicit opt-in: it prints normalized final text after redaction, not raw event
streams or child diagnostics.

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
- **`Runner profile ... does not exist` or a missing-profile config error:** add the exact named profile under `[runners.<name>]`, or change the step/global `runner` to an existing name. Profile names are strict; environment overrides do not create profiles.
- **Selected runner capability check fails:** install the documented CLI version/flags. Claude's required `--permission-mode`, `--tools`, and `--allowedTools` contract is checked before invocation; it does not silently downgrade. Codex and OpenCode similarly fail when their required adapter contract cannot run.
- **Provider/model authentication fails:** for OpenCode, run `opencode auth list`, authenticate a built-in provider, and verify the selected profile's model. Built-in auth plugins remain available, while project/global custom-provider configuration is intentionally not loaded. Codex, Claude, and custom commands own their normal login/provider setup; ai-push-hooks never invokes login. See [OpenCode isolation limits](#opencode-isolation-limits).
- **`no final response`, timeout, signal, or nonzero runner error:** the selected profile, adapter type, and stage are reported with bounded redacted diagnostics. Check that the CLI is usable from the configured cwd and that its final output contract is enabled; do not expect child stderr or raw event streams to be printed.
- **Invalid JSON after retries:** `json_max_retries` defaults to `2`. The schema retry feedback is bounded by `invalid_json_feedback_max_chars` and, by default, each retry starts a fresh invocation (`json_retry_new_session = true`). If session reuse is requested but unsupported or no session was captured, the run explicitly falls back to a fresh invocation.
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

Repository diffs, selected context, artifacts, and prompts may be sent by the
selected runner to its configured model provider. `project_access = "project"`
can expose substantially more repository content than the artifact-only default;
review provider data-handling, retention, and billing terms before using a
sensitive repository. Authentication belongs to the user and the selected CLI:
OpenCode retains its existing auth state and recognized provider environment
variables, while Codex, Claude, and custom commands inherit the normal user
environment/home they require. ai-push-hooks does not log environment values or
broker credentials.

OpenCode transcripts are exported to private local storage by default under
`.git/ai-push-hooks/transcripts`, and its sessions are deleted after each run by
default. Export is best effort: if it fails, the run warns and does not claim a
transcript exists. Codex and Claude use ephemeral/no-persistence modes and
command profiles have no inferred transcript. A local transcript never proves
provider-side deletion. See [SECURITY.md](SECURITY.md) for reporting, the threat
model, data handling, and sandbox limitations.

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

The recorded environment details are intentionally not used as compatibility
proof for every platform. Python 3.10–3.13 and Node 18+ remain declared
compatibility ranges, and the generated hook requires a POSIX shell.

The current direct smoke command uses a loopback mock provider. Do not read this
as live-provider evidence or as proof of network or operating-system isolation.

Final pinned-Lefthook verification passed with no skips:

```bash
mise exec lefthook@2.1.9 -- python -m pytest tests -q
```

```text
407 passed, 0 skipped
```

Ruff 0.13.3 also passed after the three direct-script `E402` fixes:

```bash
uv tool run --from ruff==0.13.3 ruff check --isolated .
```

Wheel, sdist, Twine 6.1.0, and npm packaging checks passed; the npm 10.9.2
offline smoke used zero LLM calls. The real OpenCode 1.18.29 smoke test passed
with an in-process loopback mock provider and no external model call; its read
probe also checks that no Git-visible project files were mutated. It is
permission/workspace evidence, not live-provider or operating-system-sandbox
evidence. Installed no-model conformance passed for OpenCode 1.18.29, Codex
0.148.0, and Claude 2.1.220 using only version/help commands. Python 3.10.18
and Python 3.12 each passed 407 tests with 0 skips; the Python 3.10.18 run used
pytest 8.3.5, build 1.2.2.post1, tomli 2.4.1, and Lefthook 2.1.9. Default tests
make no authenticated or billable model calls. The live probe rejects a
present `AI_PUSH_HOOKS_MODEL` before any setup or child call; unset it so the
explicit `--model "provider/model-id"` argument remains deliberate. See the
[verification report](docs/reports/runner-verification.md) for the full gated
Codex/Pi read and apply commands.

### Runner references

The invocation contracts were checked against the installed CLIs and their
authoritative documentation: [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive),
[Codex CLI reference](https://developers.openai.com/codex/cli/reference),
[Codex authentication](https://developers.openai.com/codex/auth),
[Codex approvals and security](https://developers.openai.com/codex/agent-approvals-security),
[Claude CLI reference](https://code.claude.com/docs/en/cli-reference),
[Claude headless mode](https://code.claude.com/docs/en/headless),
[Claude permissions](https://code.claude.com/docs/en/permissions),
[Claude sessions](https://code.claude.com/docs/en/sessions),
[Claude authentication](https://code.claude.com/docs/en/authentication), and
[Pi usage/security/providers](https://pi.dev/docs/latest/usage),
[Pi JSON mode](https://pi.dev/docs/latest/json). CLI flags and model catalogs
can change; capability checks and additive parsers are intentional.

Python 3.10–3.13 and Node 18+ remain the declared compatibility ranges, not a
claim that every patch/platform combination has passed. The generated hook
requires a POSIX shell, and Windows has no native beta evidence. On POSIX,
timeout cleanup signals a private process group on a best-effort basis; on
Windows, timeout cleanup can terminate only the direct child. Neither behavior
is a sandbox.

## Synthetic demo and evidence

For a no-secrets, no-external-model-call wiring/permission demo, run:

```bash
bash scripts/opencode-contract-smoke.sh
```

It builds a disposable image, starts only an in-process loopback mock provider,
and drives real OpenCode 1.18.29 through a synthetic repository. The Docker
runtime uses `--network none`; the image build/setup may use network access to
fetch its pinned inputs. It shows the allowlisted `README.md` edit, denied
outside/protected edits, and unchanged protected Git metadata. This is
**wiring and permission evidence only**, not a live-provider demo or OS-sandbox
claim; it exits 2 when Docker is unavailable.

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
changed-file, and repository context before `ask` query/analyze steps. An
`apply` step receives a private workspace and a narrow docs allowlist; the
assertion then blocks the push for human review and commit. `exec` and `assert`
remain available for deterministic repository actions. This ordering limits
model scope without pretending to provide an OS sandbox.

**Evidence.** The real OpenCode 1.18.29 loopback mock-provider contract found a
version-specific permission mapping (`write` requests `edit`) and covers
allowlisted propagation plus protected Git metadata without an external model
call. The final pinned-Lefthook Python suite recorded 407 passed with no skips.
Installed runner conformance is version/help-only; live Codex and Pi probes were
intentionally not run, and Claude live verification is pending.

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
- Prompt resolution precedence for `ask` and `apply` steps:
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
| `runners` | table | no | Named runner profiles; omitted for the implicit OpenCode compatibility default. |

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
| `runner` | string | `"opencode"` | Global named runner profile for `ask`/`apply`; the implicit OpenCode compatibility profile is the default. |
| `model` | string | `"openai/gpt-5.6-terra"` | Compatibility model for implicit OpenCode; explicit profiles use their own model unless overridden by `AI_PUSH_HOOKS_MODEL`. |
| `variant` | string | `""` | Optional OpenCode compatibility variant. |
| `timeout_seconds` | int | `800` | Timeout per selected runner invocation and related lifecycle calls. |
| `max_parallel` | int | `2` | Max concurrent read-only steps (`collect`, `ask`). |
| `json_max_retries` | int | `2` | Retry count for invalid JSON responses. |
| `invalid_json_feedback_max_chars` | int | `6000` | Max invalid output included in retry feedback. |
| `json_retry_new_session` | bool | `true` | Requests a fresh invocation on JSON retry; unsupported/sessionless runners always fall back fresh. |
| `delete_session_after_run` | bool | `true` | Deletes OpenCode sessions after completion. |
| `max_diff_bytes` | int | `180000` | Max bytes of git diff sent into workflow artifacts. |
| `session_title_prefix` | string | `"ai-push-hooks"` | Prefix for OpenCode session titles. |

### `[runners.<name>]`

| Key | Type | Required/default | Description |
| --- | --- | --- | --- |
| `type` | string | required | One of `opencode`, `codex`, `claude`, or `command`. |
| `model` | string | optional | Opaque runner-specific model identifier; no catalog validation is performed. |
| `project_access` | string | OpenCode: `artifacts`; others: `project` | `artifacts` or `project`; see [project visibility](#project-visibility-and-apply-boundary). |
| `variant` | string | optional, OpenCode only | OpenCode variant. |
| `command` | string array | required for `command` | Direct argv vector; no shell parsing. |
| `prompt_transport` | `stdin`/`argv` | `stdin` for `command` | `argv` requires one whole-argument `{prompt}` placeholder. |

### `[logging]`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `level` | string | `"status"` | Console verbosity (`status`, `info`, `debug`). |
| `jsonl` | bool | `true` | Enables JSONL event logging. |
| `dir` | string | `".git/ai-push-hooks/logs"` | Directory for `hook.jsonl`. |
| `capture_llm_transcript` | bool | `true` | Exports OpenCode session transcripts. |
| `transcript_dir` | string | `".git/ai-push-hooks/transcripts"` | Transcript export directory. |
| `summary_dir` | string | `".git/ai-push-hooks/summaries"` | Per-run summary JSON directory. |
| `print_llm_output` | bool | `false` | Sensitive opt-in; prints normalized, redacted final text, not raw runner events. |

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
| `type` | string | yes | all step types | One of: `collect`, `ask`, `apply`, `exec`, `assert`. Legacy `llm` and `agent` values are rejected; use `ask`. |
| `inputs` | array of strings | no | non-`collect` steps | Artifact references from earlier steps. |
| `output` | string | yes | `ask` | Output artifact filename (often `.json`). |
| `schema` | string | no | `ask` | Validates parsed model output shape. |
| `prompt` | string | conditional | `ask`, `apply` | Highest-priority prompt source. |
| `prompt_file` | string | conditional | `ask`, `apply` | Repo-relative prompt file path; absolute, traversing, and symlinked paths are rejected. |
| `fallback_prompt_id` | string | conditional | `ask`, `apply` | Built-in prompt ID used when no higher source resolves. |
| `collector` | string | yes | `collect` | Collector handler ID. |
| `allow_paths` | array of strings | yes | `apply` | File glob allowlist for edits. |
| `runner` | string | no | `ask`, `apply` | Per-step named profile override; invalid on other step types. |
| `executor` | string | yes | `exec` | Exec handler ID. |
| `assertion` | string | yes | `assert` | Assertion handler ID. |
| `when_env` | string | no | any step | Runs step only when env var parses as true. |

`ask` and `apply` are promptable step types: at least one of `prompt`, `prompt_file`, or `fallback_prompt_id` must be set.

Artifact references in `inputs` are module-local. Use `<step>/<artifact>` to reference an artifact produced by an earlier step in the same module (for example, `collect/push.diff` or `analyze/issues.json`). Cross-module references such as `docs:collect/push.diff` are not currently supported.

### Supported handler and schema values

#### Collectors

| Value | Purpose |
| --- | --- |
| `docs_context` | Collects docs-related context and diff artifacts. |
| `beads_status_context` | Collects branch/beads alignment context. |
| `pr_context` | Collects PR composition context. |

#### Ask schemas

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

Runner selection is resolved in this order: the step's `runner`, then
`[llm].runner`, then the named profile (or implicit `opencode` compatibility
profile). For the selected profile, `AI_PUSH_HOOKS_MODEL` is the final model
override. Without it, an explicit profile model wins; only the implicit
OpenCode profile inherits flat `[llm].model`. `AI_PUSH_HOOKS_VARIANT` applies
only to OpenCode and is the final override for its variant. These environment
overrides do not turn a missing profile into a valid one or change
`project_access`.

| Env var | Effect |
| --- | --- |
| `AI_PUSH_HOOKS_SKIP` | If true, sets `general.enabled = false`. |
| `AI_PUSH_HOOKS_ALLOW_PUSH_ON_ERROR` | Overrides `general.allow_push_on_error`. |
| `AI_PUSH_HOOKS_REQUIRE_CLEAN` | Overrides `general.require_clean_worktree`. |
| `AI_PUSH_HOOKS_ALLOW_DIRTY` | If true, forces `general.require_clean_worktree = false`. |
| `AI_PUSH_HOOKS_BASE_BRANCH` | Overrides `general.base_branch`. |
| `AI_PUSH_HOOKS_LOG_LEVEL` | Overrides `logging.level`. |
| `AI_PUSH_HOOKS_PRINT_LLM_OUTPUT` | Overrides `logging.print_llm_output`; normalized final text only, redacted, and sensitive. |
| `AI_PUSH_HOOKS_MODEL` | Final model override for the selected profile; values remain opaque identifiers. |
| `AI_PUSH_HOOKS_VARIANT` | Final variant override for OpenCode only. |
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
type = "ask"
fallback_prompt_id = "docs-query-basic"
inputs = ["collect/push.diff", "collect/changed-files.txt"]
output = "queries.json"
schema = "string_array"

[[modules.docs.steps]]
id = "analyze"
type = "ask"
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
type = "ask"
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
