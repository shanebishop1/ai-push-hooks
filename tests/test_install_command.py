from __future__ import annotations

import os
import pathlib
import stat
import subprocess

import pytest

from ai_push_hooks import cli, install
from ai_push_hooks.install import (
    install_hook,
    pre_push_hook_script,
    resolve_pre_push_hook_path,
)
from ai_push_hooks.types import HookError


def _git(cwd: pathlib.Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
    )
    return env


def test_install_creates_executable_repo_local_hook(repo: pathlib.Path) -> None:
    assert install_hook(False, cwd=repo) == 0
    hook_path = repo / ".git" / "hooks" / "pre-push"
    assert hook_path.read_text(encoding="utf-8") == pre_push_hook_script()
    assert hook_path.stat().st_mode & stat.S_IXUSR
    assert resolve_pre_push_hook_path(repo) == hook_path


def test_install_refuses_existing_hook_without_force_and_force_is_atomic(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook_path = repo / ".git" / "hooks" / "pre-push"
    hook_path.write_text("#!/bin/sh\nexit 17\n", encoding="utf-8")
    original = hook_path.read_bytes()

    with pytest.raises(HookError, match="without --force"):
        install_hook(False, cwd=repo)
    assert hook_path.read_bytes() == original

    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise OSError("interrupted write")

    monkeypatch.setattr(install, "atomic_write_bytes", interrupted)
    with pytest.raises(HookError, match="Could not install pre-push hook"):
        install_hook(True, cwd=repo)
    assert hook_path.read_bytes() == original


def test_install_force_replaces_regular_hook_and_rejects_special_targets(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    hook_path = repo / ".git" / "hooks" / "pre-push"
    hook_path.write_text("old\n", encoding="utf-8")
    assert install_hook(True, cwd=repo) == 0
    assert hook_path.read_text(encoding="utf-8") == pre_push_hook_script()

    hook_path.unlink()
    hook_path.mkdir()
    with pytest.raises(HookError, match="non-regular"):
        install_hook(True, cwd=repo)
    hook_path.rmdir()

    os.mkfifo(hook_path)
    with pytest.raises(HookError, match="non-regular"):
        install_hook(True, cwd=repo)
    hook_path.unlink()

    outside = tmp_path / "outside-hook"
    outside.write_text("outside\n", encoding="utf-8")
    hook_path.symlink_to(outside)
    with pytest.raises(HookError, match="symlink"):
        install_hook(True, cwd=repo)


def test_install_preserves_args_stdin_and_exit_status(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "ai-push-hooks"
    fake.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" > "$CAPTURE_ARGS"\ncat > "$CAPTURE_STDIN"\nexit 17\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    install_hook(False, cwd=repo)

    args_file = tmp_path / "args"
    stdin_file = tmp_path / "stdin"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "CAPTURE_ARGS": str(args_file),
            "CAPTURE_STDIN": str(stdin_file),
        }
    )
    completed = subprocess.run(
        [
            str(repo / ".git" / "hooks" / "pre-push"),
            "origin",
            "file:///tmp/path with spaces.git",
        ],
        input="refs/heads/main " + "a" * 40 + " refs/heads/main " + "0" * 40 + "\n",
        text=True,
        env=env,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 17
    assert (
        args_file.read_text(encoding="utf-8").strip()
        == "hook origin file:///tmp/path with spaces.git"
    )
    assert stdin_file.read_text(encoding="utf-8").startswith("refs/heads/main ")


def test_install_handles_subdirectory_and_separate_git_dir(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    nested = repo / "docs" / "nested"
    nested.mkdir()
    assert install_hook(False, cwd=nested) == 0
    assert (repo / ".git" / "hooks" / "pre-push").exists()

    separate_worktree = tmp_path / "separate-worktree"
    separate_git_dir = tmp_path / "separate-git"
    separate_worktree.mkdir()
    subprocess.run(
        ["git", "init", f"--separate-git-dir={separate_git_dir}", "."],
        cwd=separate_worktree,
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    assert install_hook(False, cwd=separate_worktree) == 0
    assert (separate_git_dir / "hooks" / "pre-push").exists()


def test_install_allows_repo_local_hooks_path_and_refuses_external_or_shared(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    before = _git(repo, "config", "--local", "--list")
    _git(repo, "config", "core.hooksPath", "custom-hooks")
    assert install_hook(False, cwd=repo) == 0
    assert (repo / "custom-hooks" / "pre-push").exists()

    external = tmp_path / "shared-hooks"
    _git(repo, "config", "core.hooksPath", str(external))
    configured = _git(repo, "config", "--local", "--list")
    with pytest.raises(HookError, match="external or shared"):
        install_hook(False, cwd=repo)
    assert _git(repo, "config", "--local", "--list") == configured
    assert configured != before


def test_install_refuses_linked_worktree_shared_hooks(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    worktree = tmp_path / "linked-worktree"
    subprocess.run(
        ["git", "worktree", "add", "-b", "linked", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    with pytest.raises(HookError, match="shared hooks path"):
        install_hook(False, cwd=worktree)


def test_install_outside_repository_and_cli_dispatch(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(HookError, match="Could not resolve Git hook location"):
        install_hook(False, cwd=tmp_path)

    called: list[bool] = []
    monkeypatch.setattr(cli, "install_hook", lambda force: called.append(force) or 0)
    assert cli.main(["install", "--force"]) == 0
    assert called == [True]
