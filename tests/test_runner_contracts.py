from __future__ import annotations

import concurrent.futures
import gc
import json
import os
import pathlib
import sys
import threading
import time

import pytest

from ai_push_hooks.executors.runners import (
    DEFAULT_MAX_OUTPUT_BYTES,
    KNOWN_RUNNER_TYPES,
    LazyRunnerSpec,
    ProcessResult,
    RunnerCapabilities,
    RunnerError,
    RunnerExecutableNotFoundError,
    RunnerMissingOutputError,
    RunnerNonzeroExitError,
    RunnerProtocolError,
    RunnerRegistry,
    RunnerRequest,
    RunnerResult,
    RunnerSignalError,
    RunnerTimeoutError,
    RunnerArtifact,
    SessionMetadata,
    build_prompt_packet,
    bounded_redacted_diagnostics,
    finalize_runner,
    require_final_text,
    require_zero_exit,
    run_process,
    strip_terminal_controls,
)


def request(tmp_path: pathlib.Path, **overrides: object) -> RunnerRequest:
    values: dict[str, object] = {
        "profile_id": "review",
        "runner_type": "command",
        "stage": "docs.query",
        "purpose": "ask:query",
        "mode": "ask",
        "instruction": "Summarize the change.",
        "artifacts": (
            RunnerArtifact("first.txt", "first body"),
            RunnerArtifact("second.txt", "second body"),
        ),
        "cwd": tmp_path,
        "timeout_seconds": 2,
    }
    values.update(overrides)
    return RunnerRequest(**values)


def test_request_is_flat_and_packet_inputs_remain_ordered(tmp_path: pathlib.Path) -> None:
    value = request(tmp_path)

    assert value.profile_id == "review"
    assert value.runner_type == "command"
    assert [artifact.name for artifact in value.artifacts] == ["first.txt", "second.txt"]
    assert [artifact.content for artifact in value.artifacts] == ["first body", "second body"]
    assert "first body" not in repr(value)
    assert "second body" not in repr(value)


def test_prompt_packet_rendering_preserves_instruction_and_artifact_order(tmp_path: pathlib.Path) -> None:
    value = request(tmp_path)
    packet = build_prompt_packet(value).render()

    assert packet.index("first.txt") < packet.index("second.txt")
    assert "first body" in packet and "second body" in packet
    assert value.instruction in packet


def test_registry_has_only_static_known_types_and_loads_lazily(tmp_path: pathlib.Path) -> None:
    loaded: list[str] = []

    class FakeRunner:
        capabilities = RunnerCapabilities()

        def run(self, _request: RunnerRequest) -> RunnerResult:
            return RunnerResult("ok", 0, "", "")

    def factory() -> FakeRunner:
        loaded.append("command")
        return FakeRunner()

    specs = {
        name: LazyRunnerSpec("unused") for name in KNOWN_RUNNER_TYPES
    }
    specs["command"] = factory
    registry = RunnerRegistry(specs)

    assert registry.known_types == KNOWN_RUNNER_TYPES
    assert loaded == []
    assert registry.get("command").run(request(tmp_path)).final_text == "ok"
    assert loaded == ["command"]
    assert registry.get("command") is registry.get("command")
    with pytest.raises(ValueError, match="unknown runner type"):
        registry.get("not-a-runner")


def test_result_and_session_metadata_do_not_claim_ephemeral_transcript_as_persisted() -> None:
    result = RunnerResult(
        final_text="done",
        returncode=0,
        stdout="raw",
        stderr="",
        session=SessionMetadata(session_id="s-1", state="ephemeral"),
    )

    assert result.session is not None
    assert result.session.state == "ephemeral"
    assert result.session.resumable is False
    assert "raw" not in repr(result)

    with pytest.raises(ValueError, match="only persisted"):
        SessionMetadata(session_id="s-1", state="deleted", resumable=True)


def test_optional_finalize_capability_is_a_noop_when_not_supported(tmp_path: pathlib.Path) -> None:
    class NoLifecycle:
        capabilities = RunnerCapabilities()

        def run(self, _request: RunnerRequest) -> RunnerResult:
            return RunnerResult("done", 0, "", "")

    value = request(tmp_path)
    result = RunnerResult("done", 0, "", "")
    assert finalize_runner(NoLifecycle(), value, result) is result


def test_finalize_capability_must_return_a_runner_result(tmp_path: pathlib.Path) -> None:
    class BadLifecycle:
        capabilities = RunnerCapabilities(supports_finalize=True)

        def finalize(self, _request: RunnerRequest, _result: RunnerResult) -> object:
            return object()

    with pytest.raises(RunnerProtocolError, match="finalizer"):
        finalize_runner(BadLifecycle(), request(tmp_path), RunnerResult("done", 0, "", ""))


def test_diagnostics_are_bounded_redacted_and_do_not_need_environment_or_prompt(tmp_path: pathlib.Path) -> None:
    diagnostic = bounded_redacted_diagnostics(
        "prompt body api_key=super-secret " + "x" * 20,
        "Authorization: Bearer bearer-secret",
        max_chars=80,
        secrets=("super-secret", "bearer-secret"),
    )

    assert len(diagnostic) <= 80
    assert "super-secret" not in diagnostic
    assert "bearer-secret" not in diagnostic
    assert "prompt body" in diagnostic

    value = request(tmp_path, instruction="do not leak this prompt")
    assert "do not leak this prompt" not in repr(value)


def test_result_accepts_multiline_final_text_and_ansi_child_output() -> None:
    result = RunnerResult(
        final_text="## Summary\n- one\n- two",
        returncode=0,
        stdout="\x1b[32mchild output\x1b[0m\n",
        stderr="\x1b]0;secret title\x07warning\n",
    )

    assert result.final_text == "## Summary\n- one\n- two"
    assert result.stdout.startswith("\x1b[32m")
    assert strip_terminal_controls(result.stderr) == "warning\n"


def test_failure_diagnostics_redact_echoed_packet_and_credential_formats(
    tmp_path: pathlib.Path,
) -> None:
    value = request(
        tmp_path,
        instruction="Never expose prompt-secret",
        artifacts=(RunnerArtifact("input.txt", "artifact-secret"),),
    )
    echoed = (
        value.prompt_packet().render()
        + '\n{"api_key": "json-secret", "OPENAI_API_KEY": "env-secret"}\n'
        + "\x1b[31mterminal-secret\x1b[0m"
    )
    result = RunnerResult("", 7, echoed, echoed)

    with pytest.raises(RunnerNonzeroExitError) as error:
        require_zero_exit(
            value,
            result,
            env={"OPENAI_API_KEY": "env-secret", "TERMINAL_TOKEN": "terminal-secret"},
        )

    message = str(error.value)
    for secret in (
        "prompt-secret",
        "artifact-secret",
        "json-secret",
        "env-secret",
        "terminal-secret",
    ):
        assert secret not in message
    assert "[REDACTED]" in message
    assert "\x1b" not in message


def test_failure_diagnostics_redact_json_escaped_packet_and_credentials(
    tmp_path: pathlib.Path,
) -> None:
    value = request(
        tmp_path,
        instruction='line "prompt-secret"\\nnext',
        artifacts=(RunnerArtifact("input.txt", "artifact-secret\\value"),),
    )
    escaped_packet = json.dumps(value.prompt_packet().render())
    escaped_error = json.dumps(
        {
            "message": escaped_packet,
            "authorization": "Bearer bearer-secret",
            "OPENAI_API_KEY": "env-secret",
        }
    )

    with pytest.raises(RunnerNonzeroExitError) as error:
        require_zero_exit(
            value,
            RunnerResult("", 9, escaped_error, escaped_error),
            env={"OPENAI_API_KEY": "env-secret"},
        )

    message = str(error.value)
    for secret in ("prompt-secret", "artifact-secret", "bearer-secret", "env-secret"):
        assert secret not in message
    assert "[REDACTED]" in message


def test_large_request_diagnostics_are_suppressed_before_redaction_scans(
    tmp_path: pathlib.Path,
) -> None:
    large_artifact = "diff-line\n" * 20_000
    value = request(tmp_path, artifacts=(RunnerArtifact("diff", large_artifact),))

    with pytest.raises(RunnerNonzeroExitError) as error:
        require_zero_exit(
            value,
            RunnerResult("", 9, "diff-line\ncredential-secret", ""),
        )

    message = str(error.value)
    assert "diagnostic output suppressed for a large request" in message
    assert "credential-secret" not in message


def test_shell_free_process_execution_captures_stdin_and_separate_streams(tmp_path: pathlib.Path) -> None:
    result = run_process(
        [
            sys.executable,
            "-c",
            "import sys; print('\\x1b[32m' + sys.stdin.read() + '\\x1b[0m', end=''); print('diagnostic', file=sys.stderr)",
        ],
        cwd=tmp_path,
        input_text="ordered packet",
        timeout_seconds=2,
    )

    assert isinstance(result, ProcessResult)
    assert result.returncode == 0
    assert result.stdout == "\x1b[32mordered packet\x1b[0m"
    assert result.stderr.strip() == "diagnostic"


def test_process_default_capture_is_agent_sized_and_per_call_bound_remains_available(
    tmp_path: pathlib.Path,
) -> None:
    assert DEFAULT_MAX_OUTPUT_BYTES >= 8 * 1024 * 1024
    result = run_process(
        [sys.executable, "-c", "print('x' * 1000)"],
        cwd=tmp_path,
        timeout_seconds=2,
        max_output_bytes=32,
    )

    assert len(result.stdout) <= 32
    assert result.stdout_truncated is True


@pytest.mark.skipif(
    os.name != "posix" or not pathlib.Path("/bin/echo").is_file(),
    reason="descriptor reuse regression requires POSIX /bin/echo",
)
def test_repeated_concurrent_processes_preserve_output_and_close_pipe_owners(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ai_push_hooks.executors.runners.process as process_module

    real_popen = process_module.subprocess.Popen
    pipe_streams: list[object] = []
    pipe_streams_lock = threading.Lock()

    def tracking_popen(*args: object, **kwargs: object) -> object:
        process = real_popen(*args, **kwargs)
        with pipe_streams_lock:
            pipe_streams.extend((process.stdin, process.stdout, process.stderr))
        return process

    monkeypatch.setattr(process_module.subprocess, "Popen", tracking_popen)

    def invoke(index: int) -> str:
        expected = f"descriptor-message-{index}\n"
        result = run_process(
            ["/bin/echo", f"descriptor-message-{index}"],
            cwd=tmp_path,
            timeout_seconds=2,
        )
        assert result.stderr == ""
        assert result.stdout == expected
        return result.stdout

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        outputs = list(executor.map(invoke, range(64)))

    assert outputs == [f"descriptor-message-{index}\n" for index in range(64)]
    assert len(pipe_streams) == 64 * 3
    assert all(getattr(stream, "closed", False) for stream in pipe_streams)

    # Releasing every prior pipe wrapper must not let a destructor close a
    # descriptor that a subsequent invocation has reused.
    pipe_streams.clear()
    gc.collect()
    assert invoke(64) == "descriptor-message-64\n"


def test_process_errors_classify_not_found_timeout_and_signal(tmp_path: pathlib.Path) -> None:
    with pytest.raises(RunnerExecutableNotFoundError):
        run_process([str(tmp_path / "missing-executable")], cwd=tmp_path, timeout_seconds=1)

    with pytest.raises(RunnerTimeoutError) as timeout_error:
        run_process(
            [
                sys.executable,
                "-c",
                "import sys, time; print('{\"sessionID\":\"timeout-session\"}', flush=True); time.sleep(10)",
            ],
            cwd=tmp_path,
            timeout_seconds=0.05,
        )
    timeout_result = getattr(timeout_error.value, "_process_result")
    assert timeout_result.stdout.strip() == '{"sessionID":"timeout-session"}'
    assert "timeout-session" not in str(timeout_error.value)
    assert "timeout-session" not in repr(timeout_error.value)

    with pytest.raises(RunnerSignalError) as signal_error:
        run_process(
            [
                sys.executable,
                "-c",
                "import os, signal, sys; print('signal-secret', flush=True); os.kill(os.getpid(), signal.SIGTERM)",
            ],
            cwd=tmp_path,
            timeout_seconds=2,
        )
    signal_result = getattr(signal_error.value, "_process_result")
    assert signal_result.stdout.strip() == "signal-secret"
    assert "signal-secret" not in str(signal_error.value)
    assert "signal-secret" not in repr(signal_error.value)


def test_process_start_errors_do_not_echo_raw_exception_arguments(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_to_start(*_args: object, **_kwargs: object) -> None:
        raise OSError("api_key=exception-secret")

    monkeypatch.setattr("ai_push_hooks.executors.runners.process.subprocess.Popen", fail_to_start)
    with pytest.raises(RunnerError) as error:
        run_process([sys.executable], cwd=tmp_path, timeout_seconds=1)
    assert "exception-secret" not in str(error.value)


def _active_pid(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if sys.platform.startswith("linux"):
        stat_path = pathlib.Path(f"/proc/{pid}/stat")
        try:
            state = stat_path.read_text(encoding="utf-8").split(") ", 1)[1].split(" ", 1)[0]
        except (FileNotFoundError, IndexError):
            return False
        return state != "Z"
    return True


def _wait_for_pid_exit(pid: int, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while _active_pid(pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    return not _active_pid(pid)


@pytest.mark.skipif(os.name != "posix", reason="process-group regression requires POSIX semantics")
def test_cleanup_kills_descendant_holding_pipes_after_leader_exits(
    tmp_path: pathlib.Path,
) -> None:
    pid_file = tmp_path / "descendant.pid"
    child_code = "import time; time.sleep(30)"
    parent_code = (
        "import subprocess, sys; "
        f"child = subprocess.Popen([{sys.executable!r}, '-c', {child_code!r}]); "
        f"open({str(pid_file)!r}, 'w', encoding='ascii').write(str(child.pid))"
    )
    started = time.monotonic()
    result = run_process(
        [sys.executable, "-c", parent_code],
        cwd=tmp_path,
        timeout_seconds=1,
    )
    elapsed = time.monotonic() - started

    child_pid = int(pid_file.read_text(encoding="ascii"))
    assert result.returncode == 0
    assert elapsed < 2.5
    assert _wait_for_pid_exit(child_pid)


@pytest.mark.skipif(os.name != "posix", reason="process-group regression requires POSIX semantics")
def test_cleanup_kills_term_ignoring_grandchild_after_parent_signal_exit(
    tmp_path: pathlib.Path,
) -> None:
    pid_file = tmp_path / "grandchild.pid"
    child_code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    parent_code = (
        "import os, signal, subprocess, sys; "
        f"child = subprocess.Popen([{sys.executable!r}, '-c', {child_code!r}]); "
        f"open({str(pid_file)!r}, 'w', encoding='ascii').write(str(child.pid)); "
        "os.kill(os.getpid(), signal.SIGTERM)"
    )
    started = time.monotonic()
    with pytest.raises(RunnerSignalError):
        run_process(
            [sys.executable, "-c", parent_code],
            cwd=tmp_path,
            timeout_seconds=1,
        )
    elapsed = time.monotonic() - started

    child_pid = int(pid_file.read_text(encoding="ascii"))
    assert elapsed < 2.5
    assert _wait_for_pid_exit(child_pid)


def test_nonzero_and_missing_final_output_are_distinct_contract_failures(tmp_path: pathlib.Path) -> None:
    value = request(tmp_path)
    result = RunnerResult("", 7, "", "token=hidden")
    with pytest.raises(RunnerNonzeroExitError, match="review.*command.*docs.query"):
        require_zero_exit(value, result, secrets=("hidden",))
    with pytest.raises(RunnerMissingOutputError):
        require_final_text("", mode="ask")
    assert require_final_text("", mode="apply") == ""
