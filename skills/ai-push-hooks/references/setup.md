# Setup And Hook Integration

## Install

Requires Git and Python 3.10+ on the hook process's `PATH` (documented support range: 3.10-3.13); npm additionally needs Node 18+. The npm wrapper does not bundle Python. Choose one installation surface:

```bash
# Repository-local npm (beta channel).
npm install --save-dev ai-push-hooks@beta
npx --no-install ai-push-hooks init --template minimal-docs
npx --no-install ai-push-hooks install
```

```bash
# Repository-local pnpm.
pnpm add -D ai-push-hooks@beta
pnpm exec ai-push-hooks init --template minimal-docs
pnpm exec ai-push-hooks install
```

```bash
# Python-only; uv tool install or pipx install can replace pip.
python -m pip install ai-push-hooks
ai-push-hooks init --template minimal-docs
ai-push-hooks install
```

Remaining references use `ai-push-hooks`; prefix with `npx --no-install` or `pnpm exec` for local installs. Keep the installed interpreter/package at its installed location.

Install and authenticate the selected AI CLI separately and choose a model available to its account. Deterministic-only workflows need no AI CLI. See [runner configuration](configuration.md#runners).

`init` writes root `ai-push-hooks.toml`. Its only template, `minimal-docs` (also the default), collects documentation context, analyzes drift, applies Markdown fixes, and blocks after edits for manual commit. It is not a generic rules-only review; adapt it using [workflows](workflows.md). Existing config is refused unless `init --force`; merge deliberately instead of overwriting customizations.

## Integrate Safely

Before `install`, inspect the effective hook and any manager configuration:

```bash
git rev-parse --show-toplevel
git status --short
git config --get core.hooksPath
git rev-parse --git-path hooks/pre-push
```

An unset `core.hooksPath` is normal. Read the returned hook if it exists. `install` writes the repository-local pre-push delegate without changing Git configuration; it refuses existing hooks, shared/external paths, and symlinks. `install --force` replaces a regular hook, not merges it. Prefer the existing manager over a second installer.

For Lefthook, add this entry to `lefthook.yml` instead of running `ai-push-hooks install`:

```yaml
pre-push:
  commands:
    ai-checks:
      run: npx --no-install ai-push-hooks hook {1} {2}
      use_stdin: true
```

Run `lefthook install`. For Python-only installs, omit the npm prefix. All managers must preserve Git's remote name/URL arguments, ref-update stdin, and nonzero exit status. If another hook command consumes stdin, capture and replay it for each consumer.

## Disable Or Remove

There is no `uninstall` subcommand. Inspect the effective hook path and remove only the generated ai-push-hooks delegate or its manager entry; preserve other hooks and configuration. `[general].enabled = false` disables execution after config loads. `AI_PUSH_HOOKS_SKIP=1 git push` skips before config loads, only for an explicitly authorized bypass, never as a test success.

Next: [verify installation](testing.md) before any real push.
