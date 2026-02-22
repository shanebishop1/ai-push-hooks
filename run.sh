#!/usr/bin/env bash

set -euo pipefail

if [[ "${AI_DOC_SYNC_SKIP:-0}" == "1" ]]; then
  printf '[ai-doc-sync] Skipped (AI_DOC_SYNC_SKIP=1).\n' >&2
  exit 0
fi

repo_root="$(git rev-parse --show-toplevel)"
script_path="${repo_root}/scripts/ai-doc-sync/hook.py"

if [[ ! -f "${script_path}" ]]; then
  printf '[ai-doc-sync] Missing hook script: %s\n' "${script_path}" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  printf '[ai-doc-sync] python3 is required but not installed.\n' >&2
  exit 1
fi

exec python3 "${script_path}" "$@"
