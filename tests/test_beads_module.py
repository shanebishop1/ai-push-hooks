from __future__ import annotations

import os
import pathlib

import pytest

from ai_push_hooks.artifacts import ArtifactStore
from ai_push_hooks.engine import WorkflowEngine
from ai_push_hooks.types import HookError, ModuleConfig, StepConfig

from .conftest import build_context, init_repo, make_config


def beads_config(enabled: bool = True):
    return make_config(
        [
            ModuleConfig(
                id="beads",
                enabled=enabled,
                steps=(
                    StepConfig(id="collect", type="collect", collector="beads_status_context"),
                    StepConfig(
                        id="plan",
                        type="llm",
                        inputs=["collect/branch-context.txt", "collect/changed-files.txt", "collect/push.diff", "collect/commits.txt"],
                        output="beads-plan.json",
                        schema="beads_alignment_result",
                        prompt="plan",
                    ),
                    StepConfig(id="apply", type="exec", executor="beads_alignment", inputs=["plan/beads-plan.json"]),
                    StepConfig(id="assert", type="assert", assertion="beads_alignment_clean", inputs=["plan/beads-plan.json"]),
                ),
            )
        ]
    )


def test_beads_disabled_skips_cleanly(tmp_path: pathlib.Path) -> None:
    repo = init_repo(tmp_path, branch="feature/beads")
    config = beads_config(enabled=False)
    context = build_context(repo, config)
    result = WorkflowEngine(context=context, artifacts=ArtifactStore(context.run_dir)).run()
    assert result.modules == {}


def test_beads_unresolved_writes_actionable_report(tmp_path: pathlib.Path) -> None:
    repo = init_repo(tmp_path, branch="feature/beads")
    config = beads_config()
    context = build_context(repo, config, ranges=[], changed_files=["src/app.py"], diff_text="+change\n")

    def fake_llm(context, step, prompt, input_paths, stage_name):
        return {
            "commands": [],
            "unresolved": True,
            "report_markdown": "# Beads Status Alignment Required\n",
        }

    engine = WorkflowEngine(
        context=context,
        artifacts=ArtifactStore(context.run_dir),
        llm_executor=fake_llm,
    )
    with pytest.raises(HookError, match="manual action"):
        engine.run()
    assert (repo / "BEADS_STATUS_ACTION_REQUIRED.md").exists()


def test_beads_non_feature_branch_skips(tmp_path: pathlib.Path) -> None:
    repo = init_repo(tmp_path, branch="main")
    config = beads_config()
    context = build_context(repo, config, ranges=[], changed_files=["src/app.py"], diff_text="+change\n")
    calls = {"llm": 0}

    def fake_llm(context, step, prompt, input_paths, stage_name):
        calls["llm"] += 1
        return {"commands": [], "unresolved": False, "report_markdown": ""}

    WorkflowEngine(
        context=context,
        artifacts=ArtifactStore(context.run_dir),
        llm_executor=fake_llm,
    ).run()
    assert calls["llm"] == 0
