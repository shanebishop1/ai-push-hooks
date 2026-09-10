from __future__ import annotations

import os
import pathlib
import signal
import sys

import pytest

from ai_push_hooks import git_utils
from ai_push_hooks.executors.runners import (
    ProcessResult,
    RunnerExecutableNotFoundError,
    RunnerSignalError,
    RunnerTimeoutError,
)
from ai_push_hooks.types import HookError


def _python(script: str, *arguments: str) -> list[str]:
    return [sys.executable, "-c", script, *arguments]


def _error_with_result(error: BaseException, result: ProcessResult) -> BaseException:
    setattr(error, "_process_result", result)
    return error


def _raise(error: BaseException) -> None:
    raise error


def test_run_command_uses_finite_default_and_forwards_text_arguments(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run_process(args: list[str], **kwargs: object) -> ProcessResult:
        captured["args"] = args
        captured.update(kwargs)
        return ProcessResult(0, "output", "diagnostic")

    monkeypatch.setattr(git_utils, "run_process", fake_run_process)

    result = git_utils.run_command(
        ["git", "status"],
        cwd=tmp_path,
        input_text="café\n",
        timeout=None,
        max_output_bytes=123,
    )

    assert result.args == ["git", "status"]
    assert result.returncode == 0
    assert result.stdout == "output"
    assert result.stderr == "diagnostic"
    assert captured == {
        "args": ["git", "status"],
        "cwd": tmp_path,
        "input_text": "café\n",
        "timeout_seconds": 120,
        "env": os.environ.copy(),
        "max_output_bytes": 123,
    }


def test_run_command_merges_environment_and_supports_removals(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_UTILS_INHERITED", "inherited")
    captured: list[dict[str, str]] = []

    def fake_run_process(_args: list[str], **kwargs: object) -> ProcessResult:
        captured.append(kwargs["env"])  # type: ignore[arg-type]
        return ProcessResult(0, "", "")

    monkeypatch.setattr(git_utils, "run_process", fake_run_process)

    git_utils.run_command(
        ["git", "status"],
        cwd=tmp_path,
        env={"GIT_UTILS_ADDED": "added", "GIT_UTILS_INHERITED": None},
    )
    merged = captured.pop()
    assert merged.get("GIT_UTILS_ADDED") == "added"
    assert "GIT_UTILS_INHERITED" not in merged
    assert merged.get("PATH") == os.environ.get("PATH")

    git_utils.run_command(
        ["git", "status"],
        cwd=tmp_path,
        env={"GIT_UTILS_ADDED": "added"},
        inherit_env=False,
    )
    assert captured.pop() == {"GIT_UTILS_ADDED": "added"}


def test_run_command_preserves_utf8_and_surrogateescape_stdin(
    tmp_path: pathlib.Path,
) -> None:
    input_text = "café\nraw-\udcff"
    result = git_utils.run_command(
        _python("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"),
        cwd=tmp_path,
        input_text=input_text,
    )

    assert result.stdout == input_text
    assert result.stderr == ""


def test_run_command_returns_nonzero_when_check_is_false(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        git_utils,
        "run_process",
        lambda *_args, **_kwargs: ProcessResult(7, "partial", "failure"),
    )

    result = git_utils.run_command(["git", "status"], cwd=tmp_path, check=False)

    assert result.returncode == 7
    assert result.stdout == "partial"
    assert result.stderr == "failure"


def test_run_command_returns_signal_result_when_check_is_false(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_result = ProcessResult(-signal.SIGTERM, "partial", "signal output")
    error = _error_with_result(RunnerSignalError("private signal details"), process_result)
    monkeypatch.setattr(git_utils, "run_process", lambda *_args, **_kwargs: _raise(error))

    result = git_utils.run_command(["git", "status"], cwd=tmp_path, check=False)

    assert result.returncode == -signal.SIGTERM
    assert result.stdout == "partial"
    assert result.stderr == "signal output"


@pytest.mark.parametrize(
    "error_factory, expected",
    [
        (
            lambda: RunnerExecutableNotFoundError("missing executable with secret-token"),
            "Command executable was not found",
        ),
        (
            lambda: _error_with_result(
                RunnerTimeoutError("timeout details with secret-token"),
                ProcessResult(9, "secret-token stdout", "secret-token stderr"),
            ),
            "Command timed out",
        ),
    ],
)
def test_run_command_normalizes_spawn_and_timeout_errors_without_leaking_details(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    error_factory: object,
    expected: str,
) -> None:
    error = error_factory()  # type: ignore[operator]
    monkeypatch.setattr(git_utils, "run_process", lambda *_args, **_kwargs: _raise(error))

    with pytest.raises(HookError) as raised:
        git_utils.run_command(
            ["tool", "--token", "secret-token"],
            cwd=tmp_path,
            input_text="secret-token",
            check=False,
        )

    assert expected in str(raised.value)
    assert "secret-token" not in str(raised.value)
    assert len(str(raised.value)) < 4_100


def test_run_command_check_failure_is_bounded_and_redacted(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "failure-secret"
    result = ProcessResult(3, "stdout " + secret, "stderr " + secret + "\n" + "x" * 20_000)
    monkeypatch.setattr(git_utils, "run_process", lambda *_args, **_kwargs: result)

    with pytest.raises(HookError, match="Command failed") as raised:
        git_utils.run_command(
            ["tool", "--password", secret],
            cwd=tmp_path,
            input_text=secret,
            check=True,
        )

    message = str(raised.value)
    assert secret not in message
    assert len(message) < 4_100


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_run_bounded_text_command_rejects_overflow_with_redacted_bounded_error(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    stream: str,
) -> None:
    secret = "overflow-secret"
    result = ProcessResult(
        0,
        "output " + secret if stream == "stdout" else "",
        "error " + secret if stream == "stderr" else "",
        stdout_truncated=stream == "stdout",
        stderr_truncated=stream == "stderr",
    )
    monkeypatch.setattr(git_utils, "run_process", lambda *_args, **_kwargs: result)

    with pytest.raises(HookError, match="capture limit") as raised:
        git_utils._run_bounded_text_command(
            ["tool", "--secret", secret],
            tmp_path,
            max_bytes=4,
            input_text=secret,
        )

    message = str(raised.value)
    assert secret not in message
    assert len(message) < 4_100


def test_bounded_text_command_forwards_custom_stderr_bound(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run_process(_args: list[str], **kwargs: object) -> ProcessResult:
        captured.update(kwargs)
        return ProcessResult(0, "ok", "")

    monkeypatch.setattr(git_utils, "run_process", fake_run_process)

    result = git_utils._run_bounded_text_command(
        ["git", "diff"], tmp_path, max_bytes=17, max_stderr_bytes=23
    )

    assert result.stdout == "ok"
    assert captured["timeout_seconds"] == 120
    assert captured["max_output_bytes"] == 17
    assert captured["max_stderr_bytes"] == 23


def test_collect_diff_keeps_partial_capture_budget_and_stderr_bound(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    def fake_run_process(_args: list[str], **kwargs: object) -> ProcessResult:
        calls.append(kwargs)
        return ProcessResult(
            0,
            "partial text",
            "stderr must not become diff output",
            stdout_truncated=True,
            stdout_bytes=b"partial text",
            stderr_bytes=b"stderr must not become diff output",
        )

    monkeypatch.setattr(git_utils, "run_process", fake_run_process)

    maximum = 80
    diff = git_utils.collect_diff(tmp_path, ["base..head"], maximum)

    assert len(diff.encode("utf-8", errors="surrogateescape")) <= maximum
    assert "### RANGE base..head\n" in diff
    assert "[diff truncated]" in diff
    assert "stderr must not become diff output" not in diff
    assert calls == [
        {
            "cwd": tmp_path,
            "timeout_seconds": 120,
            "max_output_bytes": maximum - len("### RANGE base..head\n".encode()),
            "max_stderr_bytes": git_utils.GIT_ERROR_BYTES,
        }
    ]


def test_git_utils_import_graph_exposes_one_shared_command_wrapper() -> None:
    from ai_push_hooks.executors import exec as exec_module

    assert exec_module.git_utils.run_command is git_utils.run_command
