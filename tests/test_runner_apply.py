from __future__ import annotations

import os
import pathlib
import subprocess
from dataclasses import replace

import pytest

import ai_push_hooks.executors.apply as apply_executor
from ai_push_hooks.config import load_config
from ai_push_hooks.executors.apply import run_apply_step
from ai_push_hooks.executors.runners.contracts import RunnerResult
from ai_push_hooks.types import HookError, ModuleRuntimeState, RunnerProfile

from .conftest import build_context, init_repo


def _issues_artifact(context) -> pathlib.Path:
    path = context.run_dir / "issues.json"
    path.write_text('[{"file":"README.md","description":"stale"}]\n', encoding="utf-8")
    return path


def _runner_config(config, *, project_access: str):
    profile = RunnerProfile(
        name="apply-runner",
        type="command",
        model="test-model",
        project_access=project_access,
        command=("test-runner",),
    )
    return replace(config, runners={"apply-runner": profile})


def _run(context, step, input_path):
    # Direct executor tests supply one synthetic artifact instead of the full
    # engine-resolved input list.
    step = replace(step, inputs=("issues.json",))
    return run_apply_step(
        context,
        ModuleRuntimeState(module=context.config.modules["docs"]),
        step,
        "apply prompt",
        [input_path],
        "docs.apply",
    )


def _success() -> RunnerResult:
    return RunnerResult(final_text="", returncode=0, stdout="", stderr="")


def test_project_apply_projection_is_broad_but_propagation_stays_allowlisted(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    (repo / ".gitignore").write_text("ignored.txt\nnested/ignored.txt\n", encoding="utf-8")
    (repo / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    (repo / "nested").mkdir()
    (repo / "nested" / "ignored.txt").write_text("tracked but ignored\n", encoding="utf-8")
    (repo / "nested" / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    os.mkfifo(repo / "special.fifo")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (repo / "nested-link.txt").symlink_to(outside)
    subprocess.run(
        ["git", "add", ".gitignore", "nested/AGENTS.md", "nested-link.txt"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "-f", "ignored.txt", "nested/ignored.txt"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "add project exclusions"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    config, _ = load_config(repo)
    config = _runner_config(config, project_access="project")
    context = build_context(repo, config)
    step = replace(config.modules["docs"].steps[3], runner="apply-runner")
    input_path = _issues_artifact(context)
    observed: dict[str, pathlib.Path] = {}

    def fake_runner(*args, **kwargs):
        staging = kwargs["working_directory"]
        observed["staging"] = staging
        assert (staging / "README.md").exists()
        assert (staging / "docs" / "INDEX.md").exists()
        assert (staging / "src" / "app.py").exists()
        assert (staging / "ai-push-hooks.toml").exists()
        assert not (staging / "ignored.txt").exists()
        assert not (staging / "nested" / "ignored.txt").exists()
        assert not (staging / "nested-link.txt").exists()
        assert not (staging / "nested" / "AGENTS.md").exists()
        assert not (staging / "special.fifo").exists()
        assert not (staging / ".git").exists()
        (staging / "README.md").write_text("# Project-aware update\n", encoding="utf-8")
        return _success()

    monkeypatch.setattr(apply_executor, "run_runner_once", fake_runner)

    result = _run(context, step, input_path)

    assert result["changed_files"] == ["README.md"]
    assert (repo / "README.md").read_text(encoding="utf-8") == "# Project-aware update\n"
    assert observed["staging"] != repo


def test_project_apply_rejects_nonallowlisted_mutation_before_propagation(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = _runner_config(config, project_access="project")
    context = build_context(repo, config)
    step = replace(config.modules["docs"].steps[3], runner="apply-runner")
    input_path = _issues_artifact(context)

    def fake_runner(*args, **kwargs):
        staging = kwargs["working_directory"]
        (staging / "src" / "app.py").write_text("must not propagate\n", encoding="utf-8")
        return _success()

    monkeypatch.setattr(apply_executor, "run_runner_once", fake_runner)

    with pytest.raises(HookError, match="outside allowlist"):
        _run(context, step, input_path)
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == "print('hi')\n"


def test_compatibility_apply_projection_remains_minimal(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context)

    def fake_runner(*args, **kwargs):
        staging = kwargs["working_directory"]
        assert (staging / "README.md").exists()
        assert (staging / "docs" / "INDEX.md").exists()
        assert not (staging / "src" / "app.py").exists()
        assert not (staging / "ai-push-hooks.toml").exists()
        assert not (staging / ".git").exists()
        (staging / "README.md").write_text("# Compatibility update\n", encoding="utf-8")
        return _success()

    monkeypatch.setattr(apply_executor, "run_runner_once", fake_runner)

    assert _run(context, step, input_path)["changed_files"] == ["README.md"]
