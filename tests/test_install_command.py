from __future__ import annotations

import pathlib
import stat
import subprocess

import pytest

from ai_push_hooks import cli
from ai_push_hooks.install import install_hook, pre_push_hook_script
from ai_push_hooks.types import HookError


def _hook_path(repo: pathlib.Path) -> pathlib.Path:
    return repo / ".git" / "hooks" / "pre-push"


def test_install_creates_pre_push_hook(repo: pathlib.Path) -> None:
    assert install_hook(False, cwd=repo) == 0
    hook_path = _hook_path(repo)
    assert hook_path.exists()
    assert hook_path.read_text(encoding="utf-8") == pre_push_hook_script()


def test_install_refuses_existing_pre_push_without_force(repo: pathlib.Path) -> None:
    hook_path = _hook_path(repo)
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    with pytest.raises(HookError, match="Refusing to overwrite existing pre-push hook without --force"):
        install_hook(False, cwd=repo)

    assert hook_path.read_text(encoding="utf-8") == "#!/bin/sh\nexit 0\n"


def test_install_force_overwrites_existing_pre_push(repo: pathlib.Path) -> None:
    hook_path = _hook_path(repo)
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    assert install_hook(True, cwd=repo) == 0
    assert hook_path.read_text(encoding="utf-8") == pre_push_hook_script()


def test_install_hook_is_executable(repo: pathlib.Path) -> None:
    install_hook(False, cwd=repo)
    mode = _hook_path(repo).stat().st_mode
    assert mode & stat.S_IXUSR


def test_install_script_content_forwards_all_args() -> None:
    assert pre_push_hook_script() == '#!/bin/sh\nai-push-hooks hook "$@"\n'


def test_install_outside_git_repo_fails(tmp_path: pathlib.Path) -> None:
    with pytest.raises(HookError, match="must be run inside a Git repository"):
        install_hook(False, cwd=tmp_path)


def test_install_uses_effective_git_dir_when_dotgit_is_indirection(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "repo"
    git_dir = tmp_path / "repo.git"
    repo.mkdir()
    subprocess.run(["git", "init", f"--separate-git-dir={git_dir}", "."], cwd=repo, check=True)

    assert install_hook(False, cwd=repo) == 0
    assert (git_dir / "hooks" / "pre-push").exists()


def test_cli_install_command_dispatches_with_force(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, bool] = {"force": False}

    def _fake_install(force: bool) -> int:
        called["force"] = force
        return 0

    monkeypatch.setattr(cli, "install_hook", _fake_install)
    assert cli.main(["install", "--force"]) == 0
    assert called["force"] is True
