# ai-doc-sync-hook

AI-assisted pre-push docs/beads/PR sync hook runtime.

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

## Lefthook Usage

```yaml
pre-push:
  commands:
    ai-doc-sync:
      run: ai-doc-sync-hook {1} {2}
```

For local source checkout usage, `./run.sh` works as a wrapper entrypoint.

## Configuration

Put `.ai-doc-sync.toml` in the target repo root. If no file is present, built-in defaults are used.

Prompt file paths support:

- `prompts/*.txt` (new default)
- legacy `tools/ai-doc-sync/prompts/*.txt`
- legacy `scripts/ai-doc-sync/prompts/*.txt`

## Layout

- `src/ai_doc_sync_hook/hook.py` - runtime
- `src/ai_doc_sync_hook/prompts/` - prompt assets
- `run.sh` - source checkout wrapper
- `bin/ai-doc-sync-hook.js` - npm bin wrapper
- `.ai-doc-sync.toml` - sample config
