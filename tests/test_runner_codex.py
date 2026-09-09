from __future__ import annotations

import json
import pathlib

import pytest

from ai_push_hooks.executors.runners.codex import create_runner
from ai_push_hooks.executors.runners.contracts import (
    RunnerArtifact,
    RunnerMissingOutputError,
    RunnerNonzeroExitError,
    RunnerProtocolError,
    RunnerRequest,
    RunnerSignalError,
    RunnerTimeoutError,
)
from ai_push_hooks.executors.runners.process import ProcessResult


def make_request(
    tmp_path: pathlib.Path,
    *,
    mode: str = "llm",
    project_access: str = "project",
    model: str | None = "gpt-5.6-codex",
    instruction: str = "Review the change.",
) -> RunnerRequest:
    return RunnerRequest(
        profile_id="codex-review",
        runner_type="codex",
        stage="docs.review",
        purpose="llm:review",
        mode=mode,  # type: ignore[arg-type]
        instruction=instruction,
        artifacts=(RunnerArtifact("diff.txt", "diff body"),),
        cwd=tmp_path,
        timeout_seconds=3,
        model=model,
        project_access=project_access,  # type: ignore[arg-type]
    )


def success_stream(*messages: str) -> str:
    return "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-123"}),
            *[
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "id": f"message-{i}", "text": text},
                    }
                )
                for i, text in enumerate(messages)
            ],
            json.dumps({"type": "future.event", "new_field": "ignored"}),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
    )


def test_create_runner_builds_exact_llm_argv_and_inherits_environment(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run_process(argv: list[str], **kwargs: object) -> ProcessResult:
        captured["argv"] = argv
        captured.update(kwargs)
        return ProcessResult(0, success_stream("final response"), "")

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.codex.run_process", fake_run_process
    )
    request = make_request(tmp_path)

    result = create_runner().run(request)

    assert captured["argv"] == [
        "codex",
        "exec",
        "--json",
        "--color",
        "never",
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--cd",
        str(tmp_path),
        "--model",
        "gpt-5.6-codex",
        "-",
    ]
    assert captured["cwd"] == tmp_path
    assert captured["input_text"] == request.prompt_packet().render()
    assert captured["timeout_seconds"] == 3
    assert captured["env"] is None
    assert result.final_text == "final response"
    assert result.session is not None
    assert result.session.session_id == "thread-123"
    assert result.session.state == "ephemeral"
    assert result.session.resumable is False


def test_apply_and_artifact_only_analysis_skip_git_check_and_omit_empty_model(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv_values: list[list[str]] = []

    def fake_run_process(argv: list[str], **_kwargs: object) -> ProcessResult:
        argv_values.append(argv)
        return ProcessResult(0, success_stream("done"), "")

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.codex.run_process", fake_run_process
    )

    create_runner().run(make_request(tmp_path, mode="apply", model=None))
    create_runner().run(make_request(tmp_path, project_access="artifacts", model=""))

    assert argv_values[0][-2:] == ["--skip-git-repo-check", "-"]
    assert "--model" not in argv_values[0]
    assert argv_values[1][-2:] == ["--skip-git-repo-check", "-"]
    assert "--model" not in argv_values[1]
    assert "--sandbox" in argv_values[0]
    assert argv_values[0][argv_values[0].index("--sandbox") + 1] == "workspace-write"


def test_additive_events_use_last_completed_agent_message(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.codex.run_process",
        lambda *_args, **_kwargs: ProcessResult(0, success_stream("first", "last"), ""),
    )

    assert create_runner().run(make_request(tmp_path)).final_text == "last"


@pytest.mark.parametrize(
    "exception",
    [RunnerSignalError("child signal"), RunnerTimeoutError("child timeout")],
)
def test_signal_and_timeout_fail_closed(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    exception: Exception,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> ProcessResult:
        raise exception

    monkeypatch.setattr("ai_push_hooks.executors.runners.codex.run_process", fail)

    with pytest.raises(type(exception)):
        create_runner().run(make_request(tmp_path))


def test_nonzero_exit_is_classified_and_redacted(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.codex.run_process",
        lambda *_args, **_kwargs: ProcessResult(
            9, 'echoed prompt-secret\n{"api_key":"child-secret"}', "failed"
        ),
    )

    with pytest.raises(RunnerNonzeroExitError) as error:
        create_runner().run(make_request(tmp_path, instruction="prompt-secret"))
    assert "prompt-secret" not in str(error.value)
    assert "child-secret" not in str(error.value)


def test_malformed_or_truncated_jsonl_fails_closed_with_bounded_redacted_context(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.codex.run_process",
        lambda *_args, **_kwargs: ProcessResult(
            0, '{"type":"thread.started"}\nnot json prompt-secret', "", False, False
        ),
    )
    with pytest.raises(RunnerProtocolError) as malformed:
        create_runner().run(make_request(tmp_path, instruction="prompt-secret"))
    assert "prompt-secret" not in str(malformed.value)

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.codex.run_process",
        lambda *_args, **_kwargs: ProcessResult(
            0, success_stream("done"), "", True, False
        ),
    )
    with pytest.raises(RunnerProtocolError, match="truncated"):
        create_runner().run(make_request(tmp_path))


def test_missing_final_analysis_message_fails_even_after_terminal_success(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.codex.run_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"type":"thread.started","thread_id":"thread-123"}\n'
            '{"type":"turn.completed","usage":{}}',
            "",
        ),
    )

    with pytest.raises(RunnerMissingOutputError):
        create_runner().run(make_request(tmp_path))
