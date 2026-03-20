#!/usr/bin/env bash

set -euo pipefail

if [[ "${AI_PUSH_HOOKS_SKIP:-0}" == "1" ]]; then
  printf '[ai-push-hooks] Skipped (AI_PUSH_HOOKS_SKIP=1).\n' >&2
  exit 0
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if command -v python3 >/dev/null 2>&1; then
  py_cmd="python3"
elif command -v python >/dev/null 2>&1; then
  py_cmd="python"
else
  printf '[ai-push-hooks] python3/python is required but not installed.\n' >&2
  exit 1
fi

if [[ -d "${script_dir}/src" ]]; then
  if [[ -n "${PYTHONPATH:-}" ]]; then
    export PYTHONPATH="${script_dir}/src:${PYTHONPATH}"
  else
    export PYTHONPATH="${script_dir}/src"
  fi
fi

exec "${py_cmd}" -m ai_push_hooks "$@"
