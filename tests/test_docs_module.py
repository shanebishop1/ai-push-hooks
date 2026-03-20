from __future__ import annotations

import pathlib

import pytest

from ai_push_hooks.artifacts import ArtifactStore
from ai_push_hooks.engine import WorkflowEngine
from ai_push_hooks.executors.apply import run_apply_step
from ai_push_hooks.types import ModuleRuntimeState
from ai_push_hooks.types import HookError

from .conftest import build_context, init_repo
from ai_push_hooks.config import load_config


def test_docs_drift_detection_produces_issue_artifact(tmp_path: pathlib.Path) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    (repo / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    config, _ = load_config(repo)
    context = build_context(
        repo,
        config,
        ranges=[],
        changed_files=["src/app.py"],
        diff_text="+print('changed')\n",
    )

    def fake_llm(context, step, prompt, input_paths, stage_name):
        if step.id == "query":
            return ["README"]
        if step.id == "analyze":
            return [
                {
                    "file": "README.md",
                    "line": 1,
                    "description": "README is stale",
                    "doc_excerpt": "# Example",
                    "suggested_fix": "# Updated",
                }
            ]
        raise AssertionError(step.id)

    def fake_apply(context, state, step, prompt, input_paths, stage_name):
        return {"changed": False, "changed_files": [], "skipped": True}

    engine = WorkflowEngine(
        context=context,
        artifacts=ArtifactStore(context.run_dir),
        llm_executor=fake_llm,
        apply_executor=fake_apply,
    )
    result = engine.run()
    issues_path = result.run_dir / "docs" / "02-analyze" / "issues.json"
    assert issues_path.exists()
    assert "README is stale" in issues_path.read_text(encoding="utf-8")


def test_docs_apply_allows_only_markdown_paths(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = config.modules["docs"].steps[3]
    input_path = repo / "issues.json"
    input_path.write_text('[{"file":"README.md","description":"stale"}]\n', encoding="utf-8")

    class Result:
        return_code = 0
        stderr = ""
        stdout = ""
        session_id = None

    def fake_call_opencode(context, stage_name, purpose, prompt, files, attempt=None, total_attempts=None, existing_session_id=None):
        (repo / "README.md").write_text("# Updated\n", encoding="utf-8")
        (repo / "notes.txt").write_text("bad\n", encoding="utf-8")
        return Result()

    monkeypatch.setattr("ai_push_hooks.executors.apply.call_opencode", fake_call_opencode)
    monkeypatch.setattr("ai_push_hooks.executors.apply.finalize_opencode_session", lambda *args, **kwargs: None)

    with pytest.raises(HookError, match="outside allowlist"):
        run_apply_step(
            context,
            ModuleRuntimeState(module=config.modules["docs"]),
            step,
            "prompt",
            [input_path],
            "docs.apply",
        )


def test_docs_apply_blocks_push_until_manual_commit(tmp_path: pathlib.Path) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(
        repo,
        config,
        ranges=[],
        changed_files=["src/app.py"],
        diff_text="+print('changed')\n",
    )

    def fake_llm(context, step, prompt, input_paths, stage_name):
        if step.id == "query":
            return ["README"]
        if step.id == "analyze":
            return [{"file": "README.md", "line": 1, "description": "README stale"}]
        raise AssertionError(step.id)

    def fake_apply(context, state, step, prompt, input_paths, stage_name):
        return {"changed": True, "changed_files": ["README.md"], "skipped": False}

    engine = WorkflowEngine(
        context=context,
        artifacts=ArtifactStore(context.run_dir),
        llm_executor=fake_llm,
        apply_executor=fake_apply,
    )
    with pytest.raises(HookError, match="review and commit"):
        engine.run()
