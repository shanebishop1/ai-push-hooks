from __future__ import annotations

import pathlib
import subprocess

import pytest

from ai_push_hooks.config import resolve_prompt_text
from ai_push_hooks.prompts_builtin import BUILTIN_PROMPTS
from ai_push_hooks.types import HookError, StepConfig

from .conftest import init_repo


def test_inline_prompt_wins_over_file_and_builtin(tmp_path: pathlib.Path) -> None:
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("file prompt", encoding="utf-8")
    step = StepConfig(
        id="query",
        type="llm",
        output="queries.json",
        schema="string_array",
        prompt="inline prompt",
        prompt_file="prompt.txt",
        fallback_prompt_id="docs-query-basic",
    )
    assert resolve_prompt_text(tmp_path, step) == "inline prompt"


def test_file_prompt_wins_over_builtin(tmp_path: pathlib.Path) -> None:
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("file prompt", encoding="utf-8")
    step = StepConfig(
        id="query",
        type="llm",
        output="queries.json",
        schema="string_array",
        prompt_file="prompt.txt",
        fallback_prompt_id="docs-query-basic",
    )
    assert resolve_prompt_text(tmp_path, step) == "file prompt"


def test_missing_file_falls_back_to_builtin(tmp_path: pathlib.Path) -> None:
    step = StepConfig(
        id="query",
        type="llm",
        output="queries.json",
        schema="string_array",
        prompt_file="missing.txt",
        fallback_prompt_id="docs-query-basic",
    )
    assert resolve_prompt_text(tmp_path, step) == BUILTIN_PROMPTS["docs-query-basic"]


def test_missing_all_prompt_sources_fails(tmp_path: pathlib.Path) -> None:
    step = StepConfig(id="query", type="llm", output="queries.json", schema="string_array")
    with pytest.raises(HookError, match="No prompt source available"):
        resolve_prompt_text(tmp_path, step)


@pytest.mark.parametrize("prompt_file", ["/tmp/prompt.txt", "../prompt.txt", "C:\\prompt.txt"])
def test_prompt_file_rejects_absolute_or_traversing_paths(
    tmp_path: pathlib.Path, prompt_file: str
) -> None:
    step = StepConfig(id="query", type="llm", output="result.json", prompt_file=prompt_file)

    with pytest.raises(HookError, match="Prompt file"):
        resolve_prompt_text(tmp_path, step)


def test_prompt_file_rejects_symlink_escape(tmp_path: pathlib.Path) -> None:
    outside = tmp_path.parent / "outside-prompt.txt"
    outside.write_text("unsafe", encoding="utf-8")
    (tmp_path / "prompt.txt").symlink_to(outside)
    step = StepConfig(id="query", type="llm", output="result.json", prompt_file="prompt.txt")

    with pytest.raises(HookError, match="symlink"):
        resolve_prompt_text(tmp_path, step)


@pytest.mark.parametrize("git_component", [".GiT", ".ＧＩＴ"])
def test_prompt_file_rejects_case_variant_git_component_in_primary_repo(
    tmp_path, git_component: str
) -> None:
    repo = init_repo(tmp_path, branch="feature/prompts")
    step = StepConfig(
        id="query",
        type="llm",
        output="result.json",
        prompt_file=f"{git_component}/config",
    )

    with pytest.raises(HookError, match="Git metadata"):
        resolve_prompt_text(repo, step)


def test_prompt_file_rejects_resolution_inside_nonstandard_git_dir(tmp_path) -> None:
    repo = init_repo(tmp_path, branch="feature/prompts")
    (repo / ".git").rename(repo / "repo-metadata")
    (repo / ".git").write_text("gitdir: repo-metadata\n", encoding="utf-8")
    step = StepConfig(
        id="query",
        type="llm",
        output="result.json",
        prompt_file="repo-metadata/config",
    )

    with pytest.raises(HookError, match="inside Git metadata"):
        resolve_prompt_text(repo, step)


def test_prompt_file_rejects_git_component_in_linked_worktree(tmp_path) -> None:
    primary = init_repo(tmp_path, branch="main")
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/prompts", str(linked)],
        cwd=primary,
        check=True,
        capture_output=True,
    )
    step = StepConfig(id="query", type="llm", output="result.json", prompt_file=".GIT/HEAD")

    with pytest.raises(HookError, match="Git metadata"):
        resolve_prompt_text(linked, step)
