from __future__ import annotations

import os
import pathlib
import stat
import sys

from .executors.exec import resolve_git_dir, resolve_repo_root
from .types import HookError


def pre_push_hook_script() -> str:
    return (
        "#!/bin/sh\n"
        'if [ -x "./node_modules/.bin/ai-push-hooks" ]; then\n'
        '  exec "./node_modules/.bin/ai-push-hooks" hook "$@"\n'
        "fi\n"
        'exec ai-push-hooks hook "$@"\n'
    )


def resolve_pre_push_hook_path(cwd: pathlib.Path) -> pathlib.Path:
    repo_root = resolve_repo_root(cwd)
    git_dir = resolve_git_dir(repo_root)
    return git_dir / "hooks" / "pre-push"


def install_hook(force: bool, cwd: pathlib.Path | None = None) -> int:
    current_dir = cwd or pathlib.Path.cwd()
    try:
        hook_path = resolve_pre_push_hook_path(current_dir)
    except HookError as exc:
        if "not a git repository" in str(exc).lower():
            raise HookError("`ai-push-hooks install` must be run inside a Git repository") from exc
        raise

    if hook_path.exists() and not force:
        raise HookError(f"Refusing to overwrite existing pre-push hook without --force: {hook_path}")

    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(pre_push_hook_script(), encoding="utf-8")

    mode = os.stat(hook_path).st_mode
    os.chmod(hook_path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    sys.stdout.write(str(hook_path) + "\n")
    return 0
