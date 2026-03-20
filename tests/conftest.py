from __future__ import annotations

import pathlib
import subprocess
import sys
from collections.abc import Iterable

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from ai_push_hooks.artifacts import ArtifactStore
from ai_push_hooks.executors.exec import resolve_git_dir
from ai_push_hooks.types import GeneralConfig, HookConfig, HookLogger, LlmConfig, LoggingConfig, ModuleConfig, RuntimeContext, StepConfig, WorkflowConfig


def _run(args: list[str], cwd: pathlib.Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def init_repo(tmp_path: pathlib.Path, branch: str = "main") -> pathlib.Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _run(["git", "init", "-b", "main"], repo)
    _run(["git", "config", "user.email", "codex@example.com"], repo)
    _run(["git", "config", "user.name", "Codex"], repo)
    if branch != "main":
        _run(["git", "checkout", "-b", branch], repo)
    (repo / "README.md").write_text("# Example\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "INDEX.md").write_text("# Docs Index\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-m", "init"], repo)
    return repo


def make_config(modules: Iterable[ModuleConfig]) -> HookConfig:
    module_map = {module.id: module for module in modules}
    return HookConfig(
        general=GeneralConfig(),
        llm=LlmConfig(max_parallel=2),
        logging=LoggingConfig(jsonl=False),
        workflow=WorkflowConfig(modules=tuple(module_map)),
        modules=module_map,
    )


def build_context(
    repo_root: pathlib.Path,
    config: HookConfig,
    *,
    ranges: list[str] | None = None,
    changed_files: list[str] | None = None,
    diff_text: str = "",
) -> RuntimeContext:
    git_dir = resolve_git_dir(repo_root)
    run_dir = git_dir / "ai-push-hooks-tests"
    ArtifactStore(run_dir).prepare()
    return RuntimeContext(
        repo_root=repo_root,
        git_dir=git_dir,
        config=config,
        logger=HookLogger(jsonl_path=None),
        remote_name="origin",
        remote_url="git@example.com:test/repo.git",
        stdin_lines=[],
        run_id="test-run",
        run_dir=run_dir,
        cache={
            "ranges": ranges or [],
            "changed_files": changed_files or [],
            "diff_text": diff_text,
            "branch_name": branch_name(repo_root),
            "sync_branch": "beads-sync",
        },
    )


def branch_name(repo_root: pathlib.Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    return init_repo(tmp_path)
