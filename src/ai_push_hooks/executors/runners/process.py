"""Common shell-free child-process execution for runner adapters."""

from __future__ import annotations

import os
import pathlib
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ...paths import path_is_link_or_reparse
from .contracts import (
    RunnerError,
    RunnerExecutableNotFoundError,
    RunnerSignalError,
    RunnerTimeoutError,
    bounded_diagnostic,
)


PROCESS_CHUNK_BYTES = 64 * 1024
# Agent CLIs commonly emit multi-megabyte JSONL event streams.  This remains a
# hard per-stream bound; callers with a tighter budget can override it.
DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
PROCESS_CLEANUP_GRACE_SECONDS = 1.0


@dataclass(frozen=True, repr=False)
class ProcessResult:
    """Bounded child output.  Streams are never included in ``repr``."""

    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    # Keep the original bounded bytes for callers that persist process output.
    # The text fields above intentionally retain the runner's surrogateescape
    # decoding contract.
    stdout_bytes: bytes = b""
    stderr_bytes: bytes = b""

    def __repr__(self) -> str:
        return (
            "ProcessResult("
            f"returncode={self.returncode!r}, stdout=<redacted>, stderr=<redacted>, "
            f"stdout_truncated={self.stdout_truncated!r}, stderr_truncated={self.stderr_truncated!r})"
        )


def _read_bounded(
    stream: object,
    limit: int,
    output: bytearray,
    truncated: list[bool],
    output_lock: threading.Lock,
) -> None:
    read = getattr(stream, "read")
    try:
        while True:
            try:
                chunk = read(PROCESS_CHUNK_BYTES)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            with output_lock:
                remaining = limit - len(output)
                if remaining > 0:
                    output.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated[0] = True
    finally:
        _close_pipe(stream)


def _signal_process_group(process_group_id: int, signum: int) -> None:
    try:
        os.killpg(process_group_id, signum)
    except (OSError, ProcessLookupError):
        return


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _wait_process_until(process: subprocess.Popen[bytes], deadline: float) -> None:
    while process.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            process.wait(timeout=min(remaining, 0.05))
        except subprocess.TimeoutExpired:
            continue


def _stop_process(process: subprocess.Popen[bytes], deadline: float) -> None:
    """Terminate this invocation's process group within one shared deadline."""

    process_group_id = process.pid if os.name == "posix" else None
    if process_group_id is not None:
        # start_new_session=True makes the leader's pid the private process
        # group id.  Do not gate this on leader.poll(): descendants can retain
        # the pipes after the leader has already exited.
        _signal_process_group(process_group_id, signal.SIGTERM)
    elif process.poll() is None:
        process.terminate()

    _wait_process_until(process, deadline)

    if process_group_id is not None:
        while _process_group_exists(process_group_id):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _signal_process_group(process_group_id, signal.SIGKILL)
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
                break
            time.sleep(min(0.01, remaining))
    elif process.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass


def _close_pipe(stream: object) -> None:
    """Close an unbuffered pipe through its owning stream object."""

    try:
        getattr(stream, "close")()
    except (OSError, ValueError, AttributeError):
        return


def _finish_capture(
    streams: tuple[object, ...],
    threads: tuple[threading.Thread, ...],
    deadline: float,
) -> None:
    """Bound all joins and force-close pipe fds if a reader remains blocked."""

    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(timeout=remaining)
    if any(thread.is_alive() for thread in threads):
        for stream in streams:
            _close_pipe(stream)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
    # Popen creates FileIO objects because bufsize=0. FileIO.close() does not
    # flush buffered data and atomically relinquishes descriptor ownership, so
    # repeated cleanup cannot later close a descriptor reused by another child.
    for stream in streams:
        _close_pipe(stream)


def _captured_result(
    returncode: int,
    stdout: bytearray,
    stderr: bytearray,
    stdout_truncated: list[bool],
    stderr_truncated: list[bool],
    stdout_lock: threading.Lock,
    stderr_lock: threading.Lock,
) -> ProcessResult:
    """Snapshot bounded streams for an exception without exposing them in it."""

    with stdout_lock:
        stdout_text = bytes(stdout).decode("utf-8", errors="surrogateescape")
    with stderr_lock:
        stderr_text = bytes(stderr).decode("utf-8", errors="surrogateescape")
    return ProcessResult(
        returncode=returncode,
        stdout=stdout_text,
        stderr=stderr_text,
        stdout_truncated=stdout_truncated[0],
        stderr_truncated=stderr_truncated[0],
        stdout_bytes=bytes(stdout),
        stderr_bytes=bytes(stderr),
    )


def run_process(
    argv: Sequence[str],
    *,
    cwd: pathlib.Path,
    input_text: str | None = None,
    input_path: pathlib.Path | None = None,
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
    if input_text is not None and input_path is not None:
        raise RunnerError("runner input_text and input_path are mutually exclusive")
    input_file = None
    if input_path is not None:
        if not isinstance(input_path, pathlib.Path):
            input_path = pathlib.Path(input_path)
        if path_is_link_or_reparse(input_path):
            raise RunnerError("runner input file must not be a symlink or reparse point")
        descriptor = -1
        try:
            # O_NONBLOCK prevents opening a FIFO from waiting for a writer;
            # O_NOFOLLOW closes the symlink race between validation and open.
            flags = (
                os.O_RDONLY
                | os.O_NONBLOCK
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(input_path, flags)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RunnerError("runner input file must be an ordinary regular file")
            input_file = os.fdopen(descriptor, "rb", buffering=0)
            descriptor = -1
        except RunnerError:
            raise
        except (OSError, ValueError) as exc:
            raise RunnerError("runner input file could not be opened safely") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    popen_kwargs: dict[str, object] = {
        "cwd": cwd,
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
        "env": dict(env) if env is not None else None,
        # Buffered pipe wrappers cannot safely be bypassed with os.close(): the
        # wrapper still owns the descriptor and may close a later reused fd in
        # its destructor. Unbuffered FileIO makes close bounded and ownership
        # explicit while the reader threads retain concurrent stream draining.
        "bufsize": 0,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(list(argv), **popen_kwargs)
    except FileNotFoundError as exc:
        if input_file is not None:
            input_file.close()
        raise RunnerExecutableNotFoundError(
            "runner executable was not found",
            details=bounded_diagnostic(argv[0]),
        ) from exc
    except OSError as exc:
        if input_file is not None:
            input_file.close()
        raise RunnerError("runner process could not be started", details=type(exc).__name__) from exc

    if process.stdout is None or process.stderr is None or process.stdin is None:
        _stop_process(process, time.monotonic() + PROCESS_CLEANUP_GRACE_SECONDS)
        if input_file is not None:
            input_file.close()
        raise RunnerError("runner process pipes were not available")

    stdout = bytearray()
    stderr = bytearray()
    stdout_lock = threading.Lock()
    stderr_lock = threading.Lock()
    stdout_truncated = [False]
    stderr_truncated = [False]
    stdout_thread = threading.Thread(
        target=_read_bounded,
        args=(process.stdout, max_output_bytes, stdout, stdout_truncated, stdout_lock),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_read_bounded,
        args=(process.stderr, max_output_bytes, stderr, stderr_truncated, stderr_lock),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    input_bytes = None if input_text is None else input_text.encode("utf-8", errors="surrogateescape")

    def write_input() -> None:
        try:
            if input_file is not None:
                while True:
                    chunk = input_file.read(PROCESS_CHUNK_BYTES)
                    if not chunk:
                        break
                    offset = 0
                    while offset < len(chunk):
                        written = process.stdin.write(chunk[offset:])
                        if not written:
                            return
                        offset += written
            elif input_bytes:
                offset = 0
                while offset < len(input_bytes):
                    written = process.stdin.write(input_bytes[offset:])
                    if not written:
                        break
                    offset += written
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            if input_file is not None:
                _close_pipe(input_file)
            _close_pipe(process.stdin)

    input_thread = threading.Thread(target=write_input, daemon=True)
    input_thread.start()
    returncode: int | None = None
    timeout_cause: subprocess.TimeoutExpired | None = None
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        timeout_cause = exc
    finally:
        cleanup_deadline = time.monotonic() + PROCESS_CLEANUP_GRACE_SECONDS
        _stop_process(process, cleanup_deadline)
        _finish_capture(
            (process.stdin, process.stdout, process.stderr),
            (input_thread, stdout_thread, stderr_thread),
            cleanup_deadline,
        )

    if returncode is None:
        returncode = process.poll()
    if timeout_cause is not None:
        timeout_returncode = returncode if returncode is not None else -getattr(signal, "SIGKILL", 9)
        process_result = _captured_result(
            timeout_returncode,
            stdout,
            stderr,
            stdout_truncated,
            stderr_truncated,
            stdout_lock,
            stderr_lock,
        )
        error = RunnerTimeoutError("runner process exceeded its timeout")
        setattr(error, "_process_result", process_result)
        raise error from timeout_cause

    if returncode is None:  # pragma: no cover - process.wait either returns or raises
        raise RunnerError("runner process returned no exit status")
    process_result = _captured_result(
        returncode,
        stdout,
        stderr,
        stdout_truncated,
        stderr_truncated,
        stdout_lock,
        stderr_lock,
    )
    if returncode < 0:
        error = RunnerSignalError(
            "runner process terminated by signal",
            details=str(-returncode),
        )
        setattr(error, "_process_result", process_result)
        raise error
    return process_result
