from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

from ai_push_hooks.artifacts import ArtifactStore
from ai_push_hooks.executors.runners import (
    RunnerExecutableNotFoundError,
    RunnerSignalError,
    RunnerTimeoutError,
)
from ai_push_hooks.executors.step_commands import (
    StepCommandAssertionError,
    StepCommandEncodingError,
    StepCommandError,
    StepCommandExecutionError,
    StepCommandResult,
    StepCommandTruncatedError,
    execute_step_command,
    resolve_step_command_argv,
    run_step_command,
)
from ai_push_hooks.types import ModuleConfig, ModuleRuntimeState, StepConfig


def _python(script: str, *arguments: str) -> tuple[str, ...]:
    return (sys.executable, "-c", script, *arguments)


def test_whole_argument_substitution_and_literal_braces_are_preserved(
    tmp_path: pathlib.Path,
) -> None:
    input_path = tmp_path / "input.txt"
    input_path.write_text("input", encoding="utf-8")
    command = _python(
        "import sys; print(repr(sys.argv[1:]), end='')",
        "{repo}",
        "{python}",
        "{input:input.txt}",
        "for x in {a,b}; do echo $x; done",
        "awk '{print $1}'",
        "python -c 'print({\"a\": 1})'",
    )

    rendered = resolve_step_command_argv(
        command,
        tmp_path,
        {"input.txt": input_path},
    )

    assert rendered[:3] == (sys.executable, "-c", command[2])
    assert rendered[3:6] == (str(tmp_path), sys.executable, str(input_path))
    assert rendered[6:] == command[6:]
    result = run_step_command(command, tmp_path, inputs={"input.txt": input_path})
    assert result.stdout.decode() == repr(list(rendered[3:]))


def test_explicit_bash_awk_and_python_programs_receive_braces_unchanged(
    tmp_path: pathlib.Path,
) -> None:
    bash = run_step_command(
        ("bash", "-c", "for x in {a,b}; do printf '%s' \"$x\"; done"),
        tmp_path,
    )
    awk = run_step_command(("awk", "BEGIN {print 1}"), tmp_path)
    python = run_step_command(
        (sys.executable, "-c", "print({'a': 1})"),
        tmp_path,
    )

    assert bash.stdout == b"ab"
    assert awk.stdout == b"1\n"
    assert python.stdout == b"{'a': 1}\n"


def test_rejects_unknown_and_embedded_reserved_placeholders(tmp_path: pathlib.Path) -> None:
    for argument in ("{repos}", "--path={repo}", "prefix-{input:file.txt}"):
        with pytest.raises(StepCommandError, match="placeholder"):
            resolve_step_command_argv(_python("pass", argument), tmp_path)


def test_stdin_streams_exact_bytes_and_default_stdin_is_eof(tmp_path: pathlib.Path) -> None:
    input_path = tmp_path / "stdin.bin"
    input_bytes = "Résumé ✓\n第二行\n".encode()
    input_path.write_bytes(input_bytes)
    command = _python("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())")

    streamed = run_step_command(command, tmp_path, inputs={"stdin.bin": input_path}, stdin="stdin.bin")
    empty = run_step_command(command, tmp_path)

    assert streamed.stdout == input_bytes
    assert empty.stdout == b""


def test_command_uses_repo_cwd_and_inherited_environment(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STEP_COMMAND_TEST_ENV", "inherited-value")
    result = run_step_command(
        _python("import os; print(os.getcwd() + '|' + os.environ['STEP_COMMAND_TEST_ENV'], end='')"),
        tmp_path,
    )

    assert result.stdout == f"{tmp_path}|inherited-value".encode()


def test_preserves_valid_unicode_and_rejects_invalid_utf8_bytes(tmp_path: pathlib.Path) -> None:
    valid = "Résumé ✓\n第二行"
    result = run_step_command(
        _python("import sys; sys.stdout.buffer.write('Résumé ✓\\n第二行'.encode('utf-8'))"),
        tmp_path,
    )
    assert result.stdout == valid.encode("utf-8")

    with pytest.raises(StepCommandEncodingError) as raised:
        run_step_command(_python("import sys; sys.stdout.buffer.write(b'good\\xff')"), tmp_path)
    captured = raised.value._step_command_result
    assert isinstance(captured, StepCommandResult)
    assert captured.stdout == b"good\xff"
    assert "good" not in str(raised.value)


def test_truncation_is_bounded_and_retains_both_exact_captured_streams(
    tmp_path: pathlib.Path,
) -> None:
    with pytest.raises(StepCommandTruncatedError) as raised:
        run_step_command(
            _python("import sys; sys.stdout.buffer.write(b'123456'); sys.stderr.buffer.write(b'abcdef')"),
            tmp_path,
            max_output_bytes=4,
        )
    captured = raised.value._step_command_result
    assert captured.stdout == b"1234"
    assert captured.stderr == b"abcd"
    assert captured.stdout_truncated and captured.stderr_truncated


def test_process_failures_are_named_and_capture_started_streams_without_leaking_them(
    tmp_path: pathlib.Path,
) -> None:
    with pytest.raises(RunnerTimeoutError) as timeout:
        run_step_command(
            _python("import sys, time; sys.stderr.write('timeout-secret'); sys.stderr.flush(); time.sleep(10)"),
            tmp_path,
            timeout_seconds=0.5,
        )
    assert timeout.value._process_result.stderr_bytes == b"timeout-secret"
    assert "timeout-secret" not in str(timeout.value)

    with pytest.raises(RunnerSignalError):
        run_step_command(
            _python("import os, signal; os.kill(os.getpid(), signal.SIGTERM)"),
            tmp_path,
        )
    with pytest.raises(RunnerExecutableNotFoundError):
        run_step_command((str(tmp_path / "missing-executable"),), tmp_path)


def test_zero_empty_exec_and_nonzero_exec_normalization(tmp_path: pathlib.Path) -> None:
    result = run_step_command(_python("pass"), tmp_path)
    assert result.returncode == 0 and result.stdout == b""

    step = StepConfig(id="exec", type="exec", command=_python("raise SystemExit(9)"))
    state = ModuleRuntimeState(ModuleConfig("docs", True, (step,)))
    from types import SimpleNamespace

    context = SimpleNamespace(repo_root=tmp_path, run_dir=tmp_path / "run")
    with pytest.raises(StepCommandExecutionError):
        execute_step_command(context, state, step, {}, artifacts=ArtifactStore(context.run_dir))
    assert (context.run_dir / "docs" / "00-exec" / "result.json").exists()


def test_mapping_inputs_reject_extra_references_but_duplicate_lists_remain_valid(
    tmp_path: pathlib.Path,
) -> None:
    input_path = tmp_path / "input.txt"
    input_path.write_text("input", encoding="utf-8")
    step = StepConfig(
        id="exec",
        type="exec",
        inputs=("input.txt", "input.txt"),
        command=_python("pass"),
    )
    state = ModuleRuntimeState(ModuleConfig("docs", True, (step,)))
    from types import SimpleNamespace

    context = SimpleNamespace(repo_root=tmp_path, run_dir=tmp_path / "run")
    with pytest.raises(StepCommandError, match="exactly the declared"):
        execute_step_command(
            context,
            state,
            step,
            {"input.txt": input_path, "extra.txt": input_path},
        )

    result = execute_step_command(
        context,
        state,
        step,
        [input_path, input_path],
        artifacts=ArtifactStore(context.run_dir),
    )
    assert result.result["returncode"] == 0


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO test requires POSIX mkfifo")
def test_process_input_fifo_is_rejected_without_blocking(tmp_path: pathlib.Path) -> None:
    fifo = tmp_path / "input.fifo"
    os.mkfifo(fifo)
    source_root = pathlib.Path(__file__).resolve().parents[1] / "src"
    script = """
import pathlib
import sys
from ai_push_hooks.executors.runners import RunnerError, run_process

try:
    run_process(
        (sys.executable, "-c", "pass"),
        cwd=pathlib.Path(sys.argv[2]),
        input_path=pathlib.Path(sys.argv[1]),
        timeout_seconds=1,
    )
except RunnerError:
    pass
else:
    raise SystemExit(3)
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source_root)
    completed = subprocess.run(
        [sys.executable, "-c", script, str(fifo), str(tmp_path)],
        env=environment,
        capture_output=True,
        timeout=2,
        check=False,
        text=True,
    )
    assert completed.returncode == 0


def test_assert_report_is_saved_before_failure_and_message_is_bounded_redacted(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("STEP_COMMAND_TOKEN", "assert-secret")
    step = StepConfig(
        id="policy",
        type="assert",
        command=_python("import os, sys; print('token=' + os.environ['STEP_COMMAND_TOKEN'], file=sys.stderr); raise SystemExit(3)"),
    )
    state = ModuleRuntimeState(ModuleConfig("docs", True, (step,)))
    from types import SimpleNamespace

    run_dir = repo / ".git" / "step-command-test"
    context = SimpleNamespace(repo_root=repo, run_dir=run_dir)
    with pytest.raises(StepCommandAssertionError) as raised:
        execute_step_command(context, state, step, {}, artifacts=ArtifactStore(run_dir))

    persisted = raised.value._step_command_persisted
    report_path = persisted.artifacts["result.json"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert report["returncode"] == 3
    assert "assert-secret" not in report["message"]
    assert persisted.artifacts["stderr.txt"].read_bytes() == b"token=assert-secret\n"
    assert "assert-secret" not in capsys.readouterr().err


def test_started_process_errors_persist_bounded_streams(
    repo: pathlib.Path,
) -> None:
    step = StepConfig(
        id="timeout",
        type="assert",
        command=_python("import sys, time; sys.stdout.write('partial'); sys.stdout.flush(); time.sleep(10)"),
        timeout_seconds=0.5,
    )
    state = ModuleRuntimeState(ModuleConfig("docs", True, (step,)))
    from types import SimpleNamespace

    run_dir = repo / ".git" / "step-command-timeout"
    context = SimpleNamespace(repo_root=repo, run_dir=run_dir)
    with pytest.raises(RunnerTimeoutError) as raised:
        execute_step_command(context, state, step, {}, artifacts=ArtifactStore(run_dir))
    persisted = raised.value._step_command_persisted
    assert persisted.artifacts["stdout.txt"].read_bytes() == b"partial"
    assert json.loads(persisted.artifacts["result.json"].read_text())[
        "stdout_artifact"
    ] == "stdout.txt"
