"""Execution of configuration-defined exec and assert step commands.

This module is deliberately independent from the workflow engine.  It resolves
the small command contract, delegates process lifecycle and bounded capture to
the common runner process utility, and exposes a persistence helper for the
engine integration lane.
"""

from __future__ import annotations

import math
import os
import pathlib
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..artifacts import ArtifactStore
from ..paths import path_is_link_or_reparse
from ..types import (
    DEFAULT_STEP_COMMAND_TIMEOUT_SECONDS,
    HookError,
    ModuleRuntimeState,
    RuntimeContext,
    StepConfig,
)
from .runners.contracts import bounded_diagnostic, redact_diagnostic
from .runners.process import (
    DEFAULT_MAX_OUTPUT_BYTES,
    ProcessResult,
    run_process,
)

STEP_COMMAND_STDOUT_ARTIFACT = "stdout.txt"
STEP_COMMAND_STDERR_ARTIFACT = "stderr.txt"
STEP_COMMAND_RESULT_ARTIFACT = "result.json"
_WHOLE_TOKEN = re.compile(r"\{([A-Za-z0-9_./:-]+)\}\Z")
_RECOGNIZED_TOKEN = re.compile(r"\{(?:repo|python|input:[A-Za-z0-9_./:-]+)\}")


class StepCommandError(HookError):
    """Base class for safe step-command validation and normalization errors."""


class StepCommandOutputError(StepCommandError):
    """The command exceeded its bounded output or emitted malformed text."""


class StepCommandEncodingError(StepCommandOutputError):
    """A captured command stream was not valid UTF-8."""


class StepCommandTruncatedError(StepCommandOutputError):
    """A captured command stream reached the per-stream bound."""


class StepCommandExecutionError(StepCommandError):
    """An exec command returned a non-zero status."""


class StepCommandAssertionError(StepCommandError):
    """An assert command returned a non-zero status after its report was saved."""


@dataclass(frozen=True, repr=False)
class StepCommandResult:
    """The exact bounded bytes and process status from one command invocation."""

    returncode: int
    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.returncode, bool) or not isinstance(self.returncode, int):
            raise TypeError("step command returncode must be an integer")
        if not isinstance(self.stdout, bytes) or not isinstance(self.stderr, bytes):
            raise TypeError("step command streams must be bytes")
        if (
            type(self.stdout_truncated) is not bool
            or type(self.stderr_truncated) is not bool
        ):
            raise TypeError("step command truncation flags must be booleans")

    def __repr__(self) -> str:
        return (
            "StepCommandResult("
            f"returncode={self.returncode!r}, stdout=<redacted>, stderr=<redacted>, "
            f"stdout_truncated={self.stdout_truncated!r}, "
            f"stderr_truncated={self.stderr_truncated!r})"
        )


@dataclass(frozen=True)
class PersistedStepCommandResult:
    """Result payload and private artifact paths prepared for engine dispatch."""

    result: dict[str, Any]
    artifacts: Mapping[str, pathlib.Path]


def _validate_command(command: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(command, (tuple, list)) or not command:
        raise StepCommandError("step command must be a non-empty argv vector")
    normalized: list[str] = []
    for index, argument in enumerate(command, start=1):
        if not isinstance(argument, str) or not argument or "\x00" in argument:
            raise StepCommandError(
                f"step command argument {index} must be a non-empty NUL-free string"
            )
        normalized.append(argument)
    return tuple(normalized)


def _validated_inputs(
    inputs: Mapping[str, pathlib.Path] | None,
) -> dict[str, pathlib.Path]:
    if inputs is None:
        return {}
    if not isinstance(inputs, Mapping):
        raise StepCommandError(
            "step command inputs must be a logical-reference mapping"
        )
    normalized: dict[str, pathlib.Path] = {}
    for logical_ref, path in inputs.items():
        if not isinstance(logical_ref, str) or not logical_ref:
            raise StepCommandError(
                "step command input references must be non-empty strings"
            )
        if not isinstance(path, pathlib.Path):
            path = pathlib.Path(path)
        try:
            resolved = path.resolve(strict=True)
            metadata = resolved.stat()
        except (OSError, RuntimeError) as exc:
            raise StepCommandError(
                "step command input artifact could not be opened"
            ) from exc
        if path_is_link_or_reparse(path) or not resolved.is_file() or not metadata:
            raise StepCommandError("step command input artifact must be a regular file")
        normalized[logical_ref] = resolved
    return normalized


def _substitute_argv(
    command: Sequence[str],
    *,
    repo_root: pathlib.Path,
    inputs: Mapping[str, pathlib.Path],
    python_executable: str,
) -> tuple[str, ...]:
    rendered: list[str] = []
    for index, argument in enumerate(_validate_command(command), start=1):
        match = _WHOLE_TOKEN.fullmatch(argument)
        if match is not None:
            token = match.group(1)
            if token == "repo":
                rendered.append(str(repo_root))
            elif token == "python":
                rendered.append(python_executable)
            elif token.startswith("input:"):
                logical_ref = token.removeprefix("input:")
                if logical_ref not in inputs:
                    raise StepCommandError(
                        f"step command argument {index} references an undeclared input"
                    )
                rendered.append(str(inputs[logical_ref]))
            else:
                raise StepCommandError(
                    f"unknown step command placeholder in argument {index}"
                )
            continue

        if _RECOGNIZED_TOKEN.search(argument):
            raise StepCommandError(
                f"recognized step command placeholders must be whole argv elements (argument {index})"
            )
        rendered.append(argument)
    return tuple(rendered)


def resolve_step_command_argv(
    command: Sequence[str],
    repo_root: pathlib.Path,
    inputs: Mapping[str, pathlib.Path] | None = None,
    *,
    python_executable: str | None = None,
) -> tuple[str, ...]:
    """Resolve reserved placeholders without parsing or invoking a shell."""

    try:
        root = pathlib.Path(repo_root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise StepCommandError(
            "step command repository root could not be resolved"
        ) from exc
    if not root.is_dir():
        raise StepCommandError("step command repository root must be a directory")
    executable = python_executable or sys.executable
    if not isinstance(executable, str) or not executable or "\x00" in executable:
        raise StepCommandError(
            "step command Python interpreter must be a non-empty NUL-free string"
        )
    return _substitute_argv(
        command,
        repo_root=root,
        inputs=_validated_inputs(inputs),
        python_executable=executable,
    )


def _from_process_result(result: ProcessResult) -> StepCommandResult:
    stdout_bytes = result.stdout_bytes
    stderr_bytes = result.stderr_bytes
    # Test doubles and older in-process callers may construct ProcessResult
    # without the new byte fields.  Surrogateescape preserves their text bytes.
    if not stdout_bytes and result.stdout:
        stdout_bytes = result.stdout.encode("utf-8", errors="surrogateescape")
    if not stderr_bytes and result.stderr:
        stderr_bytes = result.stderr.encode("utf-8", errors="surrogateescape")
    return StepCommandResult(
        returncode=result.returncode,
        stdout=stdout_bytes,
        stderr=stderr_bytes,
        stdout_truncated=result.stdout_truncated,
        stderr_truncated=result.stderr_truncated,
    )


def _result_from_error(error: BaseException) -> StepCommandResult | None:
    process_result = getattr(error, "_process_result", None)
    if not isinstance(process_result, ProcessResult):
        return None
    return _from_process_result(process_result)


def _validate_utf8(result: StepCommandResult) -> None:
    for name, stream in (("stdout", result.stdout), ("stderr", result.stderr)):
        try:
            stream.decode("utf-8")
        except UnicodeDecodeError as exc:
            error = StepCommandEncodingError(
                f"step command emitted invalid UTF-8 on {name}"
            )
            error._step_command_result = result
            raise error from exc


def run_step_command(
    command: Sequence[str],
    repo_root: pathlib.Path,
    *,
    inputs: Mapping[str, pathlib.Path] | None = None,
    stdin: str | None = None,
    timeout_seconds: float = DEFAULT_STEP_COMMAND_TIMEOUT_SECONDS,
    python_executable: str | None = None,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> StepCommandResult:
    """Run an exec/assert command and return bounded exact output bytes.

    The child receives the invoking environment, runs at the canonical
    repository root, and receives EOF unless ``stdin`` names an input artifact.
    Process timeout/signal/missing-executable errors retain captured output on
    their private ``_process_result`` attribute for the persistence helper.
    """

    if isinstance(timeout_seconds, bool) or not isinstance(
        timeout_seconds, (int, float)
    ):
        raise StepCommandError("step command timeout must be a finite positive number")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise StepCommandError("step command timeout must be a finite positive number")
    if (
        isinstance(max_output_bytes, bool)
        or not isinstance(max_output_bytes, int)
        or max_output_bytes < 0
    ):
        raise StepCommandError(
            "step command output bound must be a non-negative integer"
        )

    input_paths = _validated_inputs(inputs)
    if stdin is not None:
        if not isinstance(stdin, str) or stdin not in input_paths:
            raise StepCommandError(
                "step command stdin must exactly match a declared input"
            )
        input_path = input_paths[stdin]
    else:
        input_path = None
    argv = _substitute_argv(
        command,
        repo_root=pathlib.Path(repo_root).resolve(strict=True),
        inputs=input_paths,
        python_executable=python_executable or sys.executable,
    )
    process_result = run_process(
        argv,
        cwd=pathlib.Path(repo_root).resolve(strict=True),
        input_path=input_path,
        timeout_seconds=timeout_seconds,
        env=None,
        max_output_bytes=max_output_bytes,
    )
    result = _from_process_result(process_result)
    if result.stdout_truncated or result.stderr_truncated:
        streams = " and ".join(
            name
            for name, truncated in (
                ("stdout", result.stdout_truncated),
                ("stderr", result.stderr_truncated),
            )
            if truncated
        )
        error = StepCommandTruncatedError(
            f"step command {streams} exceeded its capture limit"
        )
        error._step_command_result = result
        raise error
    _validate_utf8(result)
    return result


def _environment_secrets() -> tuple[str, ...]:
    markers = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH", "CREDENTIAL")
    return tuple(
        value
        for name, value in os.environ.items()
        if any(marker in name.upper() for marker in markers) and value
    )


def _assert_message(result: StepCommandResult) -> str:
    # A process error can occur before the normal strict UTF-8 validation pass.
    # Surrogateescape keeps that diagnostic bounded and non-throwing; the
    # private stream artifacts still retain the exact original bytes.
    stdout = result.stdout.decode("utf-8", errors="surrogateescape")
    stderr = result.stderr.decode("utf-8", errors="surrogateescape")
    combined = "\n".join(part for part in (stderr.strip(), stdout.strip()) if part)
    safe = redact_diagnostic(combined, secrets=_environment_secrets())
    return (
        bounded_diagnostic(safe, max_chars=1_200)
        or f"command exited with status {result.returncode}"
    )


def step_command_result_payload(
    result: StepCommandResult,
    *,
    step_type: str,
    stdout_artifact: str = STEP_COMMAND_STDOUT_ARTIFACT,
    stderr_artifact: str = STEP_COMMAND_STDERR_ARTIFACT,
) -> dict[str, Any]:
    """Normalize a successful or process-started command into result.json data."""

    if step_type not in {"exec", "assert"}:
        raise StepCommandError("step command implementation requires exec or assert")
    payload: dict[str, Any] = {
        "returncode": result.returncode,
        "stdout_artifact": stdout_artifact,
        "stderr_artifact": stderr_artifact,
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
    }
    if step_type == "assert":
        payload["ok"] = result.returncode == 0
        if result.returncode != 0:
            payload["message"] = _assert_message(result)
    return payload


def _persist_process_result(
    store: ArtifactStore,
    state: ModuleRuntimeState,
    step: StepConfig,
    result: StepCommandResult,
    *,
    payload: dict[str, Any] | None = None,
) -> PersistedStepCommandResult:
    stdout_path = store.write_bytes(
        state,
        state.step_index,
        step.id,
        STEP_COMMAND_STDOUT_ARTIFACT,
        result.stdout,
    )
    stderr_path = store.write_bytes(
        state,
        state.step_index,
        step.id,
        STEP_COMMAND_STDERR_ARTIFACT,
        result.stderr,
    )
    report = payload or step_command_result_payload(result, step_type=step.type)
    report_path = store.write_json(
        state,
        state.step_index,
        step.id,
        STEP_COMMAND_RESULT_ARTIFACT,
        report,
    )
    return PersistedStepCommandResult(
        result=report,
        artifacts={
            STEP_COMMAND_STDOUT_ARTIFACT: stdout_path,
            STEP_COMMAND_STDERR_ARTIFACT: stderr_path,
            STEP_COMMAND_RESULT_ARTIFACT: report_path,
        },
    )


def execute_step_command(
    context: RuntimeContext,
    state: ModuleRuntimeState,
    step: StepConfig,
    inputs: Mapping[str, pathlib.Path] | Sequence[pathlib.Path],
    *,
    artifacts: ArtifactStore | None = None,
) -> PersistedStepCommandResult:
    """Run and persist one configured command for future engine dispatch.

    The report and both private stream artifacts are written before an exec
    non-zero or assert false result raises.  Process errors retain their
    bounded streams and are persisted when a process started; a missing
    executable has no process output and therefore creates no artifacts.
    """

    if step.type not in {"exec", "assert"}:
        raise StepCommandError(
            "step command implementation requires an exec or assert step"
        )
    if isinstance(inputs, Mapping):
        input_map = dict(inputs)
        declared_references = set(step.inputs)
        if set(input_map) != declared_references:
            raise StepCommandError(
                "step command input mapping must contain exactly the declared references"
            )
    else:
        if len(inputs) != len(step.inputs):
            raise StepCommandError(
                "step command input paths do not match declared inputs"
            )
        input_map = dict(zip(step.inputs, inputs))
    store = artifacts or ArtifactStore(context.run_dir)
    store.prepare()
    try:
        process_result = run_step_command(
            step.command,
            context.repo_root,
            inputs=input_map,
            stdin=step.stdin,
            timeout_seconds=(
                step.timeout_seconds
                if step.timeout_seconds is not None
                else DEFAULT_STEP_COMMAND_TIMEOUT_SECONDS
            ),
        )
    except BaseException as error:
        captured = _result_from_error(error)
        if captured is None:
            captured = getattr(error, "_step_command_result", None)
        if isinstance(captured, StepCommandResult):
            report = None
            if isinstance(error, StepCommandEncodingError):
                report = step_command_result_payload(captured, step_type=step.type)
                report["malformed"] = True
                report["message"] = "command output was not valid UTF-8"
            persisted = _persist_process_result(
                store,
                state,
                step,
                captured,
                payload=report,
            )
            error._step_command_persisted = persisted
        raise

    payload = step_command_result_payload(process_result, step_type=step.type)
    persisted = _persist_process_result(
        store, state, step, process_result, payload=payload
    )
    if step.type == "exec" and process_result.returncode != 0:
        error = StepCommandExecutionError(
            "step exec command returned a non-zero status"
        )
        error._step_command_result = process_result
        error._step_command_persisted = persisted
        raise error
    if step.type == "assert" and process_result.returncode != 0:
        error = StepCommandAssertionError(str(payload["message"]))
        error._step_command_result = process_result
        error._step_command_persisted = persisted
        raise error
    return persisted


__all__ = [
    "DEFAULT_STEP_COMMAND_TIMEOUT_SECONDS",
    "STEP_COMMAND_RESULT_ARTIFACT",
    "STEP_COMMAND_STDERR_ARTIFACT",
    "STEP_COMMAND_STDOUT_ARTIFACT",
    "PersistedStepCommandResult",
    "StepCommandAssertionError",
    "StepCommandEncodingError",
    "StepCommandError",
    "StepCommandExecutionError",
    "StepCommandOutputError",
    "StepCommandResult",
    "StepCommandTruncatedError",
    "execute_step_command",
    "resolve_step_command_argv",
    "run_step_command",
    "step_command_result_payload",
]
