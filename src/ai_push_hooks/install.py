from __future__ import annotations

import os
import pathlib
import shlex
import shutil
import stat
import subprocess
import sys

from .paths import atomic_write_bytes, is_path_within, path_is_link_or_reparse
from .types import HookError

_GIT_QUERY_TIMEOUT = 10.0
_HOOK_MODE = 0o755


def _regular_absolute_path(raw: str | None) -> str | None:
    if not raw:
        return None
    candidate = pathlib.Path(raw)
    if not candidate.is_absolute():
        return None
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.lstat()
    except (OSError, RuntimeError):
        return None
    if not stat.S_ISREG(metadata.st_mode) or path_is_link_or_reparse(resolved):
        return None
    return str(resolved)


def _delegate_argv() -> tuple[str, ...]:
    node = _regular_absolute_path(os.environ.get("AI_PUSH_HOOKS_NODE_EXECUTABLE"))
    node_script = _regular_absolute_path(os.environ.get("AI_PUSH_HOOKS_NODE_SCRIPT"))
    if node and node_script:
        return (node, node_script)

    invoked_as = pathlib.Path(sys.argv[0])
    if invoked_as.name == "ai-push-hooks":
        executable = _regular_absolute_path(str(invoked_as))
        if executable is None and not invoked_as.is_absolute():
            executable = _regular_absolute_path(shutil.which(str(invoked_as)))
        if executable:
            return (executable,)
    return ("ai-push-hooks",)


def pre_push_hook_script(delegate: tuple[str, ...] | None = None) -> str:
    """Return the small, argument/stdin/exit-status preserving hook delegate."""
    delegate = delegate or ("ai-push-hooks",)
    command = " ".join(shlex.quote(part) for part in delegate)
    if delegate == ("ai-push-hooks",):
        availability_check = (
            "if ! command -v ai-push-hooks >/dev/null 2>&1; then\n"
            "  echo '[ai-push-hooks] ai-push-hooks is not on PATH for the Git hook process.' >&2\n"
            "  echo '[ai-push-hooks] Add the Python/npm installation bin directory to PATH.' >&2\n"
            "  exit 127\n"
            "fi\n"
        )
    else:
        availability_check = ""
    return (
        "#!/bin/sh\n"
        + availability_check
        + f'exec {command} hook "$@"\n'
    )


def _git_value(cwd: pathlib.Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=_GIT_QUERY_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise HookError("Git is required for `ai-push-hooks install`") from exc
    except subprocess.TimeoutExpired as exc:
        raise HookError(f"Git command timed out while resolving hook location: {' '.join(args)}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "not a Git repository").strip()
        raise HookError(f"Could not resolve Git hook location: {detail}") from exc
    return completed.stdout.strip()


def _resolve_git_namespace(repo_root: pathlib.Path, value: str) -> pathlib.Path:
    path = pathlib.Path(value)
    return (repo_root / path).resolve() if not path.is_absolute() else path.resolve()


def _path_is_in_namespace(path: pathlib.Path, namespaces: tuple[pathlib.Path, ...]) -> bool:
    return any(is_path_within(path, namespace) for namespace in namespaces)


def _validate_parent_chain(path: pathlib.Path, namespaces: tuple[pathlib.Path, ...]) -> None:
    """Reject symlink/reparse parents and create only missing safe directories."""
    parent = path.parent
    existing: list[pathlib.Path] = []
    current = parent
    while not current.exists():
        existing.append(current)
        current = current.parent
    if path_is_link_or_reparse(current) or not current.is_dir():
        raise HookError(f"Refusing unsafe hook parent: {parent}")

    for directory in reversed(existing):
        if not _path_is_in_namespace(directory.resolve(strict=False), namespaces):
            raise HookError(f"Refusing hook parent outside the repository: {directory}")
        try:
            directory.mkdir(mode=0o755)
        except FileExistsError:
            pass
        if path_is_link_or_reparse(directory) or not directory.is_dir():
            raise HookError(f"Refusing unsafe hook parent: {directory}")

    current = parent
    while True:
        if path_is_link_or_reparse(current) or not current.is_dir():
            raise HookError(f"Refusing unsafe hook parent: {current}")
        if _path_is_in_namespace(current.resolve(strict=False), namespaces):
            break
        if current.parent == current:
            raise HookError(f"Refusing hook parent outside the repository: {parent}")
        current = current.parent


def _effective_hook_path(current_dir: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path]:
    repo_root = pathlib.Path(_git_value(current_dir, "rev-parse", "--show-toplevel")).resolve()
    git_dir = _resolve_git_namespace(repo_root, _git_value(repo_root, "rev-parse", "--git-dir"))
    common_dir = _resolve_git_namespace(
        repo_root, _git_value(repo_root, "rev-parse", "--git-common-dir")
    )
    raw_hooks_dir = pathlib.Path(_git_value(current_dir, "rev-parse", "--git-path", "hooks"))
    lexical_hooks_dir = (
        raw_hooks_dir if raw_hooks_dir.is_absolute() else current_dir / raw_hooks_dir
    )
    if path_is_link_or_reparse(current_dir) or not current_dir.is_dir():
        raise HookError(f"Refusing to install from an unsafe working directory: {current_dir}")
    if any(path_is_link_or_reparse(part) for part in lexical_hooks_dir.parents):
        raise HookError(f"Refusing hook path with a symlink or reparse parent: {lexical_hooks_dir}")
    if path_is_link_or_reparse(lexical_hooks_dir):
        raise HookError(f"Refusing hook path with a symlink or reparse parent: {lexical_hooks_dir}")
    lexical_hook_path = lexical_hooks_dir / "pre-push"
    if path_is_link_or_reparse(lexical_hook_path):
        raise HookError(f"Refusing symlink or reparse-point hook target: {lexical_hook_path}")
    hook_path = lexical_hook_path.resolve(strict=False)

    namespaces = (repo_root, git_dir)
    if not _path_is_in_namespace(hook_path, namespaces):
        raise HookError(
            "Refusing external or shared hooks path; configure a repository-local "
            "core.hooksPath instead"
        )
    if common_dir != git_dir and is_path_within(hook_path, common_dir):
        raise HookError("Refusing shared hooks path used by linked worktrees")
    return repo_root, git_dir, common_dir, hook_path


def resolve_pre_push_hook_path(cwd: pathlib.Path) -> pathlib.Path:
    """Resolve Git's effective pre-push path without changing Git configuration."""
    return _effective_hook_path(cwd.resolve())[3]


def install_hook(force: bool, cwd: pathlib.Path | None = None) -> int:
    current_dir = (cwd or pathlib.Path.cwd()).resolve()
    _repo_root, git_dir, common_dir, hook_path = _effective_hook_path(current_dir)
    if common_dir != git_dir and is_path_within(hook_path, common_dir):
        raise HookError("Refusing shared hooks path used by linked worktrees")

    namespaces = (_repo_root, git_dir)
    _validate_parent_chain(hook_path, namespaces)
    try:
        metadata = hook_path.lstat()
    except FileNotFoundError:
        metadata = None
    except OSError as exc:
        raise HookError(f"Could not inspect pre-push hook path {hook_path}: {exc}") from exc

    if metadata is not None:
        if path_is_link_or_reparse(hook_path):
            raise HookError(f"Refusing symlink or reparse-point hook target: {hook_path}")
        if not stat.S_ISREG(metadata.st_mode):
            raise HookError(f"Refusing non-regular hook target: {hook_path}")
        if not force:
            raise HookError(
                f"Refusing to overwrite existing pre-push hook without --force: {hook_path}"
            )

    try:
        atomic_write_bytes(
            hook_path,
            pre_push_hook_script(_delegate_argv()).encode("utf-8"),
            mode=_HOOK_MODE,
        )
    except HookError:
        raise
    except OSError as exc:
        raise HookError(f"Could not install pre-push hook {hook_path}: {exc}") from exc
    sys.stdout.write(str(hook_path) + "\n")
    return 0
