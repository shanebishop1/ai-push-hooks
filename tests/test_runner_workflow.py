from __future__ import annotations

import pathlib
from dataclasses import replace

import pytest

from ai_push_hooks.config import load_config
from ai_push_hooks.executors.runners import (
    RunnerCapabilities,
    RunnerError,
    RunnerResult,
    SessionMetadata,
)
from ai_push_hooks.executors.runner_workflow import run_runner_once
from ai_push_hooks.types import HookError, RunnerProfile

from .conftest import build_context, init_repo


def _logger_boundary(context, monkeypatch):
    calls = []
    completions = []

    def call(stage, purpose, model, attempt=None, total_attempts=None, **fields):
        number = len(calls) + 1
        calls.append((number, stage, purpose, model, attempt, total_attempts, fields))
        return number

    def complete(number, stage, profile, runner_type, **fields):
        completions.append((number, stage, profile, runner_type, fields))

    monkeypatch.setattr(context.logger, "llm_call", call)
    monkeypatch.setattr(context.logger, "llm_complete", complete, raising=False)
    return calls, completions


def _profiled_context(tmp_path: pathlib.Path, profile: RunnerProfile):
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config,
        llm=replace(config.llm, runner=profile.name),
        runners={profile.name: profile},
    )
    return repo, build_context(repo, config)


def test_run_runner_once_dispatches_profile_with_ordered_logical_artifacts_and_finalizes(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = RunnerProfile(name="review", type="codex", model="review-model", project_access="project")
    repo, context = _profiled_context(tmp_path, profile)
    input_one = context.run_dir / "first.txt"
    input_two = context.run_dir / "second.txt"
    input_one.write_text("first body", encoding="utf-8")
    input_two.write_text("second body", encoding="utf-8")
    step = next(step for step in context.config.modules["docs"].steps if step.id == "query")
    step = replace(step, inputs=("first.logical", "second.logical"))
    calls, completions = _logger_boundary(context, monkeypatch)
    finalized = []

    class FakeRunner:
        capabilities = RunnerCapabilities(supports_finalize=True)

        def run(self, request):
            assert request.cwd == repo.resolve()
            assert request.mode == "llm"
            assert request.model == "review-model"
            assert [(item.name, item.content) for item in request.artifacts] == [
                ("first.logical", "first body"),
                ("second.logical", "second body"),
            ]
            return RunnerResult("answer", 0, "raw", "")

        def finalize(self, _request, result):
            finalized.append(result)
            return result

    monkeypatch.setattr("ai_push_hooks.executors.runner_workflow.get_runner", lambda _type: FakeRunner())
    result = run_runner_once(
        context,
        step,
        "instruction",
        [input_one, input_two],
        "docs.query",
        working_directory=repo,
    )

    assert result.final_text == "answer"
    assert len(finalized) == 1
    assert calls[0][0] == 1
    assert completions[0][0:4] == (1, "docs.query", "review", "codex")
    assert completions[0][4]["failed"] is False


def test_run_runner_once_wraps_missing_output_and_finalizes_failure(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = RunnerProfile(name="review", type="codex")
    repo, context = _profiled_context(tmp_path, profile)
    step = next(step for step in context.config.modules["docs"].steps if step.id == "query")
    step = replace(step, inputs=())
    _logger_boundary(context, monkeypatch)
    finalized = []

    class FakeRunner:
        capabilities = RunnerCapabilities(supports_finalize=True)

        def run(self, _request):
            return RunnerResult("", 0, "", "")

        def finalize(self, _request, result):
            finalized.append(result)
            return result

    monkeypatch.setattr("ai_push_hooks.executors.runner_workflow.get_runner", lambda _type: FakeRunner())
    with pytest.raises(HookError, match=r"review.*codex.*docs\.query"):
        run_runner_once(context, step, "instruction", [], "docs.query", working_directory=repo)
    assert len(finalized) == 1


def test_runner_error_session_id_is_finalized_without_inventing_resume_command(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = RunnerProfile(name="opencode-review", type="opencode")
    repo, context = _profiled_context(tmp_path, profile)
    step = next(step for step in context.config.modules["docs"].steps if step.id == "query")
    step = replace(step, inputs=())
    _logger_boundary(context, monkeypatch)
    finalized = []

    class FakeFailure(RunnerError):
        pass

    class FakeRunner:
        capabilities = RunnerCapabilities(supports_resume=True, supports_finalize=True)

        def run(self, _request):
            error = FakeFailure("provider failed")
            error.session_id = "session-1"
            raise error

        def finalize(self, _request, result):
            finalized.append(result)
            return replace(
                result,
                session=SessionMetadata("session-1", "persisted", True),
            )

    monkeypatch.setattr("ai_push_hooks.executors.runner_workflow.get_runner", lambda _type: FakeRunner())
    with pytest.raises(HookError, match=r"opencode-review.*opencode.*docs\.query"):
        run_runner_once(context, step, "instruction", [], "docs.query", working_directory=repo)
    assert finalized[0].session is not None
    assert finalized[0].session.session_id == "session-1"
