"""Common shell-free child-process execution for runner adapters."""

from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import threading
from dataclasses import dataclass
from typing import Mapping, Sequence

from .contracts import (
    RunnerError,
    RunnerExecutableNotFoundError,
    RunnerSignalError,
    RunnerTimeoutError,
    bounded_diagnostic,
)


PROCESS_CHUNK_BYTES = 64 * 1024
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024


@dataclass(frozen=True, repr=False)
class ProcessResult:
    """Bounded child output.  Streams are never included in ``repr``."""

    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    def __repr__(self) -> str:
        return (
            "ProcessResult("
            f"returncode={self.returncode!r}, stdout=<redacted>, stderr=<redacted>, "
            f"stdout_truncated={self.stdout_truncated!r}, stderr_truncated={self.stderr_truncated!r})"
        )


def _read_bounded(stream: object, limit: int, output: bytearray, truncated: list[bool]) -> None:
    read = getattr(stream, "read")
    while True:
        chunk = read(PROCESS_CHUNK_BYTES)
        if not chunk:
            return
        remaining = limit - len(output)
        if remaining > 0:
            output.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated[0] = True


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            process.terminate()
    else:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                process.kill()
        else:
            process.kill()
        process.wait(timeout=2)


def run_process(
    argv: Sequence[str],
    *,
    cwd: pathlib.Path,
    input_text: str | None = None,
    timeout_seconds: float,
    env: Mapping[str, str] | None = None,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> ProcessResult:
    """Run an argv vector directly, with bounded concurrent stream capture.

    ``shell=False`` is explicit and prompt text is written only to stdin.  The
    environment is accepted for the child but is never inspected or included
    in errors.  Non-zero exits are returned for adapter-specific normalization;
    timeout and signal termination are raised as distinct fail-closed errors.
    """

    if not isinstance(argv, (tuple, list)) or not argv or any(
        not isinstance(argument, str) or not argument or "\x00" in argument for argument in argv
    ):
        raise RunnerError("runner command must be a non-empty NUL-free argv vector")
    if not isinstance(cwd, pathlib.Path):
        cwd = pathlib.Path(cwd)
    if not cwd.is_dir():
        raise RunnerError("runner cwd must be an existing directory")
    if timeout_seconds <= 0:
        raise RunnerError("runner timeout must be greater than zero")
    if max_output_bytes < 0:
        raise RunnerError("runner output bound must not be negative")

    popen_kwargs: dict[str, object] = {
        "cwd": cwd,
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
        "env": dict(env) if env is not None else None,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(list(argv), **popen_kwargs)
    except FileNotFoundError as exc:
        raise RunnerExecutableNotFoundError(
            "runner executable was not found",
            details=bounded_diagnostic(argv[0]),
        ) from exc
    except OSError as exc:
        raise RunnerError("runner process could not be started", details=type(exc).__name__) from exc

    if process.stdout is None or process.stderr is None or process.stdin is None:
        _stop_process(process)
        raise RunnerError("runner process pipes were not available")

    stdout = bytearray()
    stderr = bytearray()
    stdout_truncated = [False]
    stderr_truncated = [False]
    stdout_thread = threading.Thread(
        target=_read_bounded,
        args=(process.stdout, max_output_bytes, stdout, stdout_truncated),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_read_bounded,
        args=(process.stderr, max_output_bytes, stderr, stderr_truncated),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    input_bytes = None if input_text is None else input_text.encode("utf-8", errors="surrogateescape")

    def write_input() -> None:
        if input_bytes is None:
            process.stdin.close()
            return
        try:
            process.stdin.write(input_bytes)
            process.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            try:
                process.stdin.close()
            except OSError:
                pass

    input_thread = threading.Thread(target=write_input, daemon=True)
    input_thread.start()
    timed_out = False
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        try:
            _stop_process(process)
        except subprocess.TimeoutExpired as stop_error:
            raise RunnerTimeoutError("runner process did not terminate safely") from stop_error
        raise RunnerTimeoutError("runner process exceeded its timeout") from exc
    finally:
        if process.poll() is None and not timed_out:
            _stop_process(process)
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        input_thread.join(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    stdout_text = bytes(stdout).decode("utf-8", errors="surrogateescape")
    stderr_text = bytes(stderr).decode("utf-8", errors="surrogateescape")
    if returncode < 0:
        raise RunnerSignalError(
            "runner process terminated by signal",
            details=str(-returncode),
        )
    return ProcessResult(
        returncode=returncode,
        stdout=stdout_text,
        stderr=stderr_text,
        stdout_truncated=stdout_truncated[0],
        stderr_truncated=stderr_truncated[0],
    )
