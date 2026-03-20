from __future__ import annotations

import pathlib

from ai_push_hooks.artifacts import ArtifactStore
from ai_push_hooks.engine import WorkflowEngine
from ai_push_hooks.types import ModuleConfig, StepConfig

from .conftest import build_context, init_repo, make_config


def pr_config():
    return make_config(
        [
            ModuleConfig(
                id="pr",
                enabled=True,
                steps=(
                    StepConfig(id="collect", type="collect", collector="pr_context"),
                    StepConfig(
                        id="compose",
                        type="llm",
                        prompt="compose",
                        inputs=["collect/pr-context.txt", "collect/changed-files.txt", "collect/push.diff", "collect/commits.txt"],
                        output="pr-draft.json",
                        schema="pr_create_payload",
                    ),
                    StepConfig(
                        id="create",
                        type="exec",
                        executor="gh_pr_create",
                        when_env="AI_PUSH_HOOKS_CREATE_PR",
                        inputs=["compose/pr-draft.json"],
                    ),
                ),
            )
        ]
    )


def test_pr_module_skips_when_flag_not_set(tmp_path: pathlib.Path) -> None:
    repo = init_repo(tmp_path, branch="feature/pr")
    config = pr_config()
    context = build_context(repo, config, ranges=[], changed_files=["src/app.py"], diff_text="+change\n")
    calls = {"llm": 0}

    def fake_llm(context, step, prompt, input_paths, stage_name):
        calls["llm"] += 1
        return {"title": "x", "body": "y", "base_branch": "main", "head_branch": "feature/pr", "draft": False}

    WorkflowEngine(
        context=context,
        artifacts=ArtifactStore(context.run_dir),
        llm_executor=fake_llm,
    ).run()
    assert calls["llm"] == 0


def test_pr_module_composes_then_invokes_exec_when_enabled(tmp_path: pathlib.Path, monkeypatch) -> None:
    repo = init_repo(tmp_path, branch="feature/pr")
    config = pr_config()
    context = build_context(repo, config, ranges=[], changed_files=["src/app.py"], diff_text="+change\n")
    monkeypatch.setenv("AI_PUSH_HOOKS_CREATE_PR", "1")
    calls = {"llm": 0, "exec": 0}

    def fake_llm(context, step, prompt, input_paths, stage_name):
        calls["llm"] += 1
        return {"title": "My PR", "body": "Body", "base_branch": "main", "head_branch": "feature/pr", "draft": False}

    def fake_exec(context, state, step, input_paths):
        calls["exec"] += 1
        return {"pr_url": "https://github.com/example/repo/pull/1"}

    WorkflowEngine(
        context=context,
        artifacts=ArtifactStore(context.run_dir),
        llm_executor=fake_llm,
        exec_handlers={"gh_pr_create": fake_exec},
    ).run()
    assert calls == {"llm": 1, "exec": 1}
