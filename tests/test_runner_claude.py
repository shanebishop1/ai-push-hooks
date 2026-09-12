from __future__ import annotations

import json
import pathlib

import pytest

from ai_push_hooks.executors.runners import (
    RunnerArtifact,
    RunnerAdapterUnavailableError,
    RunnerMissingOutputError,
    RunnerNonzeroExitError,
    RunnerProtocolError,
    RunnerRequest,
    RunnerTimeoutError,
)
from ai_push_hooks.executors.runners.process import ProcessResult
from ai_push_hooks.executors.runners import claude


HELP = """
Usage: claude [options] [prompt]
  -p, --print
  --output-format <format> (json)
  --no-session-persistence
  --model <model>
  --permission-mode <mode> (dontAsk, acceptEdits)
  --tools <tools...>
  --allowedTools <tools...>
"""


def request(
    tmp_path: pathlib.Path, *, mode: str = "ask", **overrides: object
) -> RunnerRequest:
    values: dict[str, object] = {
        "profile_id": "claude-review",
        "runner_type": "claude",
        "stage": "docs.query",
        "purpose": "ask:query",
        "mode": mode,
        "instruction": "Summarize prompt-secret safely.",
        "artifacts": (RunnerArtifact("input.txt", "artifact-secret\nbody"),),
        "cwd": tmp_path,
        "timeout_seconds": 2,
        "model": "sonnet",
    }
    values.update(overrides)
    return RunnerRequest(**values)


def install_fake_cli(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[ProcessResult],
) -> tuple[list[dict[str, object]], str]:
    calls: list[dict[str, object]] = []
    executable = "/usr/local/bin/claude"

    monkeypatch.setattr(
        claude.shutil, "which", lambda name: executable if name == "claude" else None
    )

    def fake_run_process(
        argv: list[str],
        *,
        cwd: pathlib.Path,
        input_text: str | None,
        timeout_seconds: float,
        env: object = None,
    ) -> ProcessResult:
        calls.append(
            {
                "argv": argv,
                "cwd": cwd,
                "input_text": input_text,
                "timeout_seconds": timeout_seconds,
                "env": env,
            }
        )
        return responses.pop(0)

    monkeypatch.setattr(claude, "run_process", fake_run_process)
    return calls, executable


def help_result() -> ProcessResult:
    return ProcessResult(0, HELP, "")


def success_result(
    *, text: str = "done", session_id: str = "session-123"
) -> ProcessResult:
    return ProcessResult(
        0,
        '{"type":"result","subtype":"success","is_error":false,'
        f'"result":{json.dumps(text)},"session_id":{json.dumps(session_id)},'
        '"duration_ms":12,"future_metadata":{"ignored":true}}\n',
        "",
    )


def test_create_runner_checks_help_without_model_or_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    calls, executable = install_fake_cli(monkeypatch, [help_result()])
    runner = claude.create_runner()

    assert runner.executable == executable
    assert calls == [
        {
            "argv": [executable, "--help"],
            "cwd": pathlib.Path.cwd(),
            "input_text": None,
            "timeout_seconds": claude.CAPABILITY_CHECK_TIMEOUT_SECONDS,
            "env": None,
        }
    ]
    assert "--model" not in calls[0]["argv"]


def test_analysis_argv_stdin_cwd_and_inherited_environment_are_exact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    calls, executable = install_fake_cli(monkeypatch, [help_result(), success_result()])
    runner = claude.create_runner()
    value = request(tmp_path)
    result = runner.run(value)

    assert result.final_text == "done"
    assert calls[1] == {
        "argv": [
            executable,
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--model",
            "sonnet",
            "--permission-mode",
            "dontAsk",
            "--tools",
            "Read,Glob,Grep",
            "--allowedTools",
            "Read,Glob,Grep",
        ],
        "cwd": tmp_path,
        "input_text": value.prompt_packet().render(),
        "timeout_seconds": 2,
        "env": None,
    }


def test_apply_uses_accept_edits_and_only_read_edit_write_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    calls, executable = install_fake_cli(monkeypatch, [help_result(), success_result()])
    runner = claude.create_runner()
    runner.run(request(tmp_path, mode="apply", model=None))

    assert calls[1]["argv"] == [
        executable,
        "-p",
        "--output-format",
        "json",
        "--no-session-persistence",
        "--permission-mode",
        "acceptEdits",
        "--tools",
        "Read,Edit,Write",
        "--allowedTools",
        "Read,Edit,Write",
    ]
    assert "Bash" not in calls[1]["argv"]


def test_success_parses_additive_metadata_and_marks_session_ephemeral(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    calls, _ = install_fake_cli(monkeypatch, [help_result(), success_result()])
    result = claude.create_runner().run(request(tmp_path))

    assert result.final_text == "done"
    assert result.returncode == 0
    assert result.session is not None
    assert result.session.session_id == "session-123"
    assert result.session.state == "ephemeral"
    assert result.session.resumable is False
    assert len(calls) == 2


@pytest.mark.parametrize(
    "payload",
    [
        '{"type":"result","subtype":"error_max_turns","is_error":true,"result":"prompt-secret"}',
        '{"type":"result","subtype":"error_during_execution","is_error":false,"result":"api_key=child-secret"}',
    ],
)
def test_error_result_subtype_fails_closed_and_redacts_diagnostics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, payload: str
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-secret")
    install_fake_cli(
        monkeypatch,
        [
            help_result(),
            ProcessResult(0, payload, '{"ANTHROPIC_API_KEY":"env-secret"}'),
        ],
    )

    with pytest.raises(RunnerProtocolError) as error:
        claude.create_runner().run(request(tmp_path))

    message = str(error.value)
    assert "prompt-secret" not in message
    assert "artifact-secret" not in message
    assert "child-secret" not in message
    assert "env-secret" not in message
    assert "Claude returned an invalid result" in message
    assert "error_during_execution" not in message


@pytest.mark.parametrize(
    "stdout, truncated, match",
    [
        ("not json", False, "valid JSON"),
        ('["not an object"]', False, "object"),
        (
            '{"type":"result","subtype":"success","is_error":false}',
            False,
            "result was not a string",
        ),
        (
            '{"type":"result","subtype":"success","is_error":false,"result":"partial"}',
            True,
            "truncated",
        ),
        (
            '{"type":"result","subtype":"success","is_error":false,"result":"partial"}',
            True,
            "stderr",
        ),
        (
            '{"type":"result","subtype":"success","is_error":false,"result":"ok"}\nnoise',
            False,
            "valid JSON",
        ),
    ],
)
def test_malformed_or_truncated_protocol_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    stdout: str,
    truncated: bool,
    match: str,
) -> None:
    stderr_truncated = match == "stderr"
    install_fake_cli(
        monkeypatch,
        [
            help_result(),
            ProcessResult(
                0,
                stdout,
                "",
                stdout_truncated=truncated and not stderr_truncated,
                stderr_truncated=stderr_truncated,
            ),
        ],
    )

    with pytest.raises(RunnerProtocolError, match=match):
        claude.create_runner().run(request(tmp_path))


def test_success_with_empty_result_is_missing_analysis_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    install_fake_cli(
        monkeypatch,
        [
            help_result(),
            ProcessResult(
                0,
                '{"type":"result","subtype":"success","is_error":false,"result":""}',
                "",
            ),
        ],
    )

    with pytest.raises(RunnerMissingOutputError):
        claude.create_runner().run(request(tmp_path))


def test_nonzero_exit_is_normalized_and_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    install_fake_cli(
        monkeypatch,
        [
            help_result(),
            ProcessResult(9, "prompt-secret", "Authorization: Bearer token-secret"),
        ],
    )

    with pytest.raises(RunnerNonzeroExitError) as error:
        claude.create_runner().run(request(tmp_path))
    message = str(error.value)
    assert "prompt-secret" not in message
    assert "token-secret" not in message
    assert "claude-review" in message


def test_process_timeout_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    executable = "/usr/local/bin/claude"
    monkeypatch.setattr(claude.shutil, "which", lambda _name: executable)
    # Construction still needs capability evidence; restore one capability
    # response for that check and then make the invocation time out.
    responses = [help_result()]

    def capability_then_timeout(
        argv: list[str],
        *,
        cwd: pathlib.Path,
        input_text: str | None,
        timeout_seconds: float,
        env: object = None,
    ) -> ProcessResult:
        if argv == [executable, "--help"]:
            return responses.pop(0)
        raise RunnerTimeoutError("runner process exceeded its timeout")

    monkeypatch.setattr(claude, "run_process", capability_then_timeout)
    runner = claude.create_runner()
    with pytest.raises(RunnerTimeoutError):
        runner.run(request(tmp_path))


@pytest.mark.parametrize(
    "which_help, expected",
    [
        ("missing", "not installed"),
        ("nonzero", "help command failed"),
        ("missing-accept-edits", "missing required flags or modes"),
    ],
)
def test_capability_unavailable_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    which_help: str,
    expected: str,
) -> None:
    executable = "/usr/local/bin/claude"
    if which_help == "missing":
        monkeypatch.setattr(claude.shutil, "which", lambda _name: None)
    else:
        monkeypatch.setattr(claude.shutil, "which", lambda _name: executable)
        output = (
            HELP.replace("acceptEdits", "unsupportedMode")
            if which_help == "missing-accept-edits"
            else HELP
        )

        def fake_help(
            argv: list[str],
            *,
            cwd: pathlib.Path,
            input_text: str | None,
            timeout_seconds: float,
            env: object = None,
        ) -> ProcessResult:
            return ProcessResult(
                2 if which_help == "nonzero" else 0, output, "help failure"
            )

        monkeypatch.setattr(claude, "run_process", fake_help)

    with pytest.raises(RunnerAdapterUnavailableError, match=expected):
        claude.create_runner()
