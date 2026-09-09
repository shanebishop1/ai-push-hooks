from __future__ import annotations

import pathlib
import sys

import pytest

from ai_push_hooks.executors.runners import (
    RunnerContractError,
    RunnerExecutableNotFoundError,
    RunnerMissingOutputError,
    RunnerNonzeroExitError,
    RunnerProtocolError,
    RunnerRequest,
    RunnerSignalError,
    RunnerTimeoutError,
    RunnerArtifact,
)
from ai_push_hooks.executors.runners.command import create_runner


def _request(tmp_path: pathlib.Path, **overrides: object) -> RunnerRequest:
    values: dict[str, object] = {
        "profile_id": "pi-shaped",
        "runner_type": "command",
        "stage": "docs.query",
        "purpose": "llm:query",
        "mode": "llm",
        "instruction": "Return the answer.\nKeep the line break.",
        "artifacts": (RunnerArtifact("context.txt", "artifact body"),),
        "cwd": tmp_path,
        "timeout_seconds": 2,
        "model": "test-model",
        "command": (sys.executable, "-c", "import sys; print(sys.stdin.read(), end='')"),
    }
    values.update(overrides)
    return RunnerRequest(**values)


def _script_request(tmp_path: pathlib.Path, script: str, **overrides: object) -> RunnerRequest:
    if "command" in overrides:
        return _request(tmp_path, **overrides)
    return _request(
        tmp_path,
        command=(sys.executable, "-c", script),
        **overrides,
    )


def test_stdin_transport_sends_exact_packet_and_eof(tmp_path: pathlib.Path) -> None:
    request = _script_request(
        tmp_path,
        "import sys; data = sys.stdin.read(); print(repr(data), end='')",
    )

    result = create_runner().run(request)

    expected = request.prompt_packet().render()
    assert result.final_text == repr(expected)
    assert result.final_text == result.stdout
    assert result.stderr == ""


def test_argv_transport_replaces_prompt_and_explicitly_closes_stdin(tmp_path: pathlib.Path) -> None:
    request = _script_request(
        tmp_path,
        "import sys; print(repr(sys.argv[1:]), end=''); assert sys.stdin.buffer.read() == b''",
        prompt_transport="argv",
        command=(sys.executable, "-c", "import sys; print(repr(sys.argv[1:]), end=''); assert sys.stdin.buffer.read() == b''", "{prompt}"),
    )

    result = create_runner().run(request)

    assert result.final_text == repr([request.prompt_packet().render()])


def test_substitutes_model_cwd_and_stage_as_whole_argv_values(tmp_path: pathlib.Path) -> None:
    request = _script_request(
        tmp_path,
        "import os, sys; print(repr(sys.argv[1:]), end=''); print(os.getcwd(), end='')",
        command=(
            sys.executable,
            "-c",
            "import os, sys; print(repr(sys.argv[1:]), end=''); print(os.getcwd(), end='')",
            "{model}",
            "{cwd}",
            "{stage}",
        ),
    )

    result = create_runner().run(request)

    assert "test-model" in result.final_text
    assert str(tmp_path) in result.final_text
    assert "docs.query" in result.final_text


def test_inherits_user_environment_and_does_not_expand_shell_syntax(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMMAND_RUNNER_TEST_ENV", "inherited-value")
    request = _script_request(
        tmp_path,
        "import os, sys; print(os.environ['COMMAND_RUNNER_TEST_ENV'] + '|' + sys.argv[1], end='')",
        command=(
            sys.executable,
            "-c",
            "import os, sys; print(os.environ['COMMAND_RUNNER_TEST_ENV'] + '|' + sys.argv[1], end='')",
            "$COMMAND_RUNNER_TEST_ENV && echo expanded",
        ),
    )

    result = create_runner().run(request)

    assert result.final_text == "inherited-value|$COMMAND_RUNNER_TEST_ENV && echo expanded"


def test_pi_shaped_profile_is_only_an_argv_configuration(tmp_path: pathlib.Path) -> None:
    request = _script_request(
        tmp_path,
        "import sys; print(sys.argv[1], end='')",
        command=(sys.executable, "-c", "import sys; print(sys.argv[1], end='')", "--print", "{model}"),
    )

    assert create_runner().run(request).final_text == "--print"


@pytest.mark.parametrize(
    ("command", "transport", "model"),
    [
        (("agent", "--prompt={prompt}"), "stdin", None),
        (("agent",), "argv", None),
        (("agent", "{prompt}", "{prompt}"), "argv", None),
        (("agent", "{unknown}"), "stdin", None),
        (("agent", "prefix-{stage}"), "stdin", None),
        (("agent", "{model}"), "stdin", None),
    ],
)
def test_rejects_malformed_placeholder_requests(
    tmp_path: pathlib.Path,
    command: tuple[str, ...],
    transport: str,
    model: str | None,
) -> None:
    request = _request(
        tmp_path,
        command=command,
        prompt_transport=transport,
        model=model,
    )

    with pytest.raises(RunnerContractError):
        create_runner().run(request)


def test_rejects_malformed_programmatic_request_even_after_construction(tmp_path: pathlib.Path) -> None:
    request = _request(tmp_path)
    object.__setattr__(request, "command", ("agent", "--prompt={prompt}"))

    with pytest.raises(RunnerContractError):
        create_runner().run(request)


def test_preserves_plain_unicode_multiline_stdout_and_separates_stderr(tmp_path: pathlib.Path) -> None:
    text = "Résumé ✓\n第二行\nlast line"
    request = _script_request(
        tmp_path,
        "import sys; print('Résumé ✓\\n第二行\\nlast line', end=''); print('diagnostic', file=sys.stderr)",
    )

    result = create_runner().run(request)

    assert result.final_text == text
    assert result.stdout == text
    assert result.stderr == "diagnostic\n"


def test_empty_stdout_fails_llm_but_succeeds_for_apply(tmp_path: pathlib.Path) -> None:
    script = "import sys; print('', end='')"

    with pytest.raises(RunnerMissingOutputError):
        create_runner().run(_script_request(tmp_path, script, mode="llm"))

    result = create_runner().run(_script_request(tmp_path, script, mode="apply"))
    assert result.final_text == ""


def test_nonzero_exit_is_classified_and_redacts_prompt_model_and_environment(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COMMAND_RUNNER_TEST_TOKEN", "environment-secret")
    request = _script_request(
        tmp_path,
        "import os, sys; print(sys.stdin.read() + os.environ['COMMAND_RUNNER_TEST_TOKEN'] + ' model-secret', file=sys.stderr, end=''); raise SystemExit(9)",
        instruction="prompt-secret",
        artifacts=(RunnerArtifact("context.txt", "artifact-secret"),),
        model="model-secret",
    )

    with pytest.raises(RunnerNonzeroExitError) as error:
        create_runner().run(request)

    message = str(error.value)
    assert "pi-shaped" in message and "docs.query" in message
    for secret in ("prompt-secret", "artifact-secret", "environment-secret", "model-secret"):
        assert secret not in message
    assert "[REDACTED]" in message


def test_propagates_timeout_signal_and_missing_executable(tmp_path: pathlib.Path) -> None:
    with pytest.raises(RunnerTimeoutError):
        create_runner().run(
            _request(
                tmp_path,
                command=(sys.executable, "-c", "import time; time.sleep(10)"),
                timeout_seconds=0.05,
            )
        )

    with pytest.raises(RunnerSignalError):
        create_runner().run(
            _request(
                tmp_path,
                command=(sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"),
            )
        )

    with pytest.raises(RunnerExecutableNotFoundError):
        create_runner().run(_request(tmp_path, command=(str(tmp_path / "missing"),)))


def test_rejects_truncated_stdout_and_stderr(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_push_hooks.executors.runners import ProcessResult
    import ai_push_hooks.executors.runners.command as command_module

    monkeypatch.setattr(
        command_module,
        "run_process",
        lambda *_args, **_kwargs: ProcessResult(0, "partial", "diagnostic", stdout_truncated=True),
    )

    with pytest.raises(RunnerProtocolError, match="capture limit"):
        create_runner().run(_request(tmp_path))


def test_result_has_no_session_metadata(tmp_path: pathlib.Path) -> None:
    result = create_runner().run(_request(tmp_path))

    assert result.session is None
    assert result.transcript is None
