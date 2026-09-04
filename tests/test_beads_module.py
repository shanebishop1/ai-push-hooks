from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

from ai_push_hooks.artifacts import ArtifactStore
from ai_push_hooks.engine import WorkflowEngine
from ai_push_hooks.executors import exec as exec_module
from ai_push_hooks.executors.exec import (
    BEADS_ALIGNMENT_MAX_COMMANDS,
    BEADS_ALIGNMENT_TIMEOUT_SECONDS,
    BEADS_ALIGNMENT_TOTAL_TIMEOUT_SECONDS,
    beads_alignment_executor,
    collect_commit_messages_for_ranges,
)
from ai_push_hooks.types import HookError, ModuleConfig, ModuleRuntimeState, StepConfig

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
                        inputs=[
                            "collect/branch-context.txt",
                            "collect/changed-files.txt",
                            "collect/push.diff",
                            "collect/commits.txt",
                        ],
                        output="beads-plan.json",
                        schema="beads_alignment_result",
                        prompt="plan",
                    ),
                    StepConfig(
                        id="apply",
                        type="exec",
                        executor="beads_alignment",
                        inputs=["plan/beads-plan.json"],
                    ),
                    StepConfig(
                        id="assert",
                        type="assert",
                        assertion="beads_alignment_clean",
                        inputs=["plan/beads-plan.json"],
                    ),
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
    context = build_context(
        repo, config, ranges=[], changed_files=["src/app.py"], diff_text="+change\n"
    )

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
    context = build_context(
        repo, config, ranges=[], changed_files=["src/app.py"], diff_text="+change\n"
    )
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


def test_collect_commit_messages_handles_empty_commit_body(tmp_path: pathlib.Path) -> None:
    repo = init_repo(tmp_path, branch="feature/beads")
    target = repo / "docs" / "INDEX.md"
    target.write_text("# Docs Index\n\n- updated\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", str(target)], cwd=repo, check=True, capture_output=True, text=True
    )
    subprocess.run(
        ["git", "commit", "-m", "single line subject"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )

    previous = subprocess.run(
        ["git", "rev-parse", "HEAD~1"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    commits = collect_commit_messages_for_ranges(repo, [f"{previous}..{head}"])

    assert len(commits) == 1
    assert commits[0]["hash"] == head
    assert commits[0]["subject"] == "single line subject"
    assert commits[0]["body"] == ""


def test_beads_alignment_executes_only_validated_alignment_commands(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/beads")
    config = beads_config()
    context = build_context(repo, config)
    state = ModuleRuntimeState(module=config.modules["beads"])
    plan_path = context.run_dir / "plan.json"
    commands = [
        "bd update ai-push-hooks-123 --status in_progress",
        "bd close ai-push-hooks-456 --reason 'work shipped'",
    ]
    plan_path.write_text(json.dumps({"commands": commands}), encoding="utf-8")
    calls: list[tuple[list[str], int | None, dict[str, str], bool]] = []
    resolutions: list[pathlib.Path] = []
    monkeypatch.setenv("BD_DB", "/tmp/beads.db")
    monkeypatch.setenv("DOLT_USERNAME", "beads-user")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-pass")
    monkeypatch.setenv("PYTHONPATH", "must-not-pass")

    def fake_resolve(repo_root):
        resolutions.append(repo_root)
        return "/safe/bin/bd"

    def fake_run_command(
        args,
        cwd,
        input_text=None,
        timeout=None,
        check=False,
        env=None,
        inherit_env=True,
    ):
        calls.append((args, timeout, env, inherit_env))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("ai_push_hooks.executors.exec.resolve_beads_executable", fake_resolve)
    monkeypatch.setattr("ai_push_hooks.executors.exec.run_command", fake_run_command)

    result = beads_alignment_executor(
        context,
        state,
        StepConfig(id="apply", type="exec", executor="beads_alignment"),
        [plan_path],
    )

    assert result["commands_run"] == commands
    assert resolutions == [repo]
    assert [call[:2] for call in calls] == [
        (
            ["/safe/bin/bd", "update", "ai-push-hooks-123", "--status", "in_progress"],
            BEADS_ALIGNMENT_TIMEOUT_SECONDS,
        ),
        (
            ["/safe/bin/bd", "close", "ai-push-hooks-456", "--reason", "work shipped"],
            BEADS_ALIGNMENT_TIMEOUT_SECONDS,
        ),
    ]
    for _, _, env, inherit_env in calls:
        assert inherit_env is False
        assert env["BD_DB"] == "/tmp/beads.db"
        assert env["DOLT_USERNAME"] == "beads-user"
        assert "UNRELATED_SECRET" not in env
        assert "PYTHONPATH" not in env


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf .",
        "/usr/local/bin/bd update issue-1 --status in_progress",
        "./bd close issue-1",
        "bd --db /tmp/evil.db update issue-1 --status in_progress",
        "bd update issue-1 --status closed",
        "bd update issue-1 --status in_progress --db /tmp/evil.db",
        "bd delete issue-1",
        "bd sync",
        "bd migrate",
        "bd admin compact",
        "bd close issue-1 --force",
        "bd close issue-1; touch owned",
        "bd update 'unterminated",
    ],
)
def test_beads_alignment_rejects_untrusted_commands_before_any_execution(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    repo = init_repo(tmp_path, branch="feature/beads")
    config = beads_config()
    context = build_context(repo, config)
    state = ModuleRuntimeState(module=config.modules["beads"])
    plan_path = context.run_dir / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "commands": [
                    "bd update safe-1 --status in_progress",
                    command,
                ]
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "ai_push_hooks.executors.exec.run_command",
        lambda args, **kwargs: calls.append(args),
    )

    with pytest.raises(HookError):
        beads_alignment_executor(
            context,
            state,
            StepConfig(id="apply", type="exec", executor="beads_alignment"),
            [plan_path],
        )

    assert calls == []


def test_beads_alignment_rejects_excessive_command_count_before_execution(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/beads")
    config = beads_config()
    context = build_context(repo, config)
    state = ModuleRuntimeState(module=config.modules["beads"])
    plan_path = context.run_dir / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "commands": [
                    f"bd update issue-{index} --status in_progress"
                    for index in range(BEADS_ALIGNMENT_MAX_COMMANDS + 1)
                ]
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "ai_push_hooks.executors.exec.run_command",
        lambda args, **kwargs: calls.append(args),
    )

    with pytest.raises(HookError, match="at most"):
        beads_alignment_executor(
            context,
            state,
            StepConfig(id="apply", type="exec", executor="beads_alignment"),
            [plan_path],
        )

    assert calls == []


def test_beads_alignment_enforces_total_execution_budget(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/beads")
    config = beads_config()
    context = build_context(repo, config)
    state = ModuleRuntimeState(module=config.modules["beads"])
    plan_path = context.run_dir / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "commands": [
                    "bd update issue-1 --status in_progress",
                    "bd update issue-2 --status in_progress",
                ]
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []
    times = iter([0.0, 0.0, float(BEADS_ALIGNMENT_TOTAL_TIMEOUT_SECONDS + 1)])
    monkeypatch.setattr("ai_push_hooks.executors.exec.time.monotonic", lambda: next(times))
    monkeypatch.setattr(
        "ai_push_hooks.executors.exec.resolve_beads_executable", lambda _repo: "/safe/bin/bd"
    )
    monkeypatch.setattr(
        "ai_push_hooks.executors.exec.run_command",
        lambda args, **kwargs: calls.append(args),
    )

    with pytest.raises(HookError, match="total budget"):
        beads_alignment_executor(
            context,
            state,
            StepConfig(id="apply", type="exec", executor="beads_alignment"),
            [plan_path],
        )

    assert calls == [["/safe/bin/bd", "update", "issue-1", "--status", "in_progress"]]


def test_beads_alignment_reports_report_write_failure(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/beads")
    config = beads_config()
    context = build_context(repo, config)
    state = ModuleRuntimeState(module=config.modules["beads"])
    plan_path = context.run_dir / "plan.json"
    plan_path.write_text(
        json.dumps({"commands": [], "report_markdown": "# Manual action"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "ai_push_hooks.executors.exec.write_text_file", lambda *args, **kwargs: False
    )

    with pytest.raises(HookError, match="Failed to write Beads alignment report"):
        beads_alignment_executor(
            context,
            state,
            StepConfig(id="apply", type="exec", executor="beads_alignment"),
            [plan_path],
        )


@pytest.mark.parametrize("report_markdown", ["# replacement", ""])
def test_beads_default_report_never_follows_or_unlinks_symlink(
    tmp_path: pathlib.Path,
    report_markdown: str,
) -> None:
    repo = init_repo(tmp_path, branch="feature/beads")
    config = beads_config()
    context = build_context(repo, config)
    state = ModuleRuntimeState(module=config.modules["beads"])
    outside = tmp_path / "outside-report.md"
    outside.write_text("user content\n", encoding="utf-8")
    (repo / "BEADS_STATUS_ACTION_REQUIRED.md").symlink_to(outside)
    plan_path = context.run_dir / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "commands": [],
                "unresolved": bool(report_markdown),
                "report_markdown": report_markdown,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(HookError, match="symlink"):
        beads_alignment_executor(
            context,
            state,
            StepConfig(id="apply", type="exec", executor="beads_alignment"),
            [plan_path],
        )

    assert outside.read_text(encoding="utf-8") == "user content\n"
    assert (repo / "BEADS_STATUS_ACTION_REQUIRED.md").is_symlink()


def test_beads_configured_report_rejects_symlinked_parent(tmp_path: pathlib.Path) -> None:
    repo = init_repo(tmp_path, branch="feature/beads")
    config = beads_config()
    context = build_context(repo, config)
    state = ModuleRuntimeState(module=config.modules["beads"])
    outside = tmp_path / "outside-reports"
    outside.mkdir()
    (repo / "reports").symlink_to(outside, target_is_directory=True)
    branch_context = context.run_dir / "branch-context.txt"
    branch_context.write_text("report_file=reports/status.md\n", encoding="utf-8")
    state.artifacts["collect/branch-context.txt"] = branch_context
    plan_path = context.run_dir / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "commands": [],
                "unresolved": True,
                "report_markdown": "# replacement",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(HookError, match="symlink"):
        beads_alignment_executor(
            context,
            state,
            StepConfig(id="apply", type="exec", executor="beads_alignment"),
            [plan_path],
        )

    assert not (outside / "status.md").exists()


def test_beads_executable_resolution_rejects_repository_candidate(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/beads")
    executable = repo / "bd"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(exec_module.shutil, "which", lambda _name: str(executable))

    with pytest.raises(HookError, match="repository-contained"):
        exec_module.resolve_beads_executable(repo)
