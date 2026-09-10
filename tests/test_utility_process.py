from __future__ import annotations

import os
import pathlib
import sys
import time

import pytest

from ai_push_hooks.executors.runners import RunnerSignalError, RunnerTimeoutError, run_process


def _python(script: str, *arguments: str) -> list[str]:
    return [sys.executable, "-c", script, *arguments]


def _active_pid(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if sys.platform.startswith("linux"):
        try:
            state = (
                pathlib.Path(f"/proc/{pid}/stat")
                .read_text()
                .split(") ", 1)[1]
                .split(" ", 1)[0]
            )
        except (FileNotFoundError, IndexError):
            return False
        return state != "Z"
    return True


def _wait_for_pid_exit(pid: int, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while _active_pid(pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    return not _active_pid(pid)


def test_process_preserves_text_contract_and_bounds_each_stream(
    tmp_path: pathlib.Path,
) -> None:
    result = run_process(
        _python(
            "import sys; sys.stdout.buffer.write(b'123456'); sys.stderr.buffer.write(b'abcdef')"
        ),
        cwd=tmp_path,
        timeout_seconds=2,
        max_output_bytes=4,
    )

    assert result.returncode == 0
    assert result.stdout == "1234"
    assert result.stderr == "abcd"
    assert result.stdout_truncated and result.stderr_truncated


def test_process_stops_a_live_child_as_soon_as_output_reaches_the_bound(
    tmp_path: pathlib.Path,
) -> None:
    started = time.monotonic()
    result = run_process(
        _python(
            "import sys, time; sys.stdout.buffer.write(b'123456'); "
            "sys.stdout.flush(); time.sleep(10)"
        ),
        cwd=tmp_path,
        timeout_seconds=10,
        max_output_bytes=4,
    )

    assert result.stdout == "1234"
    assert result.stdout_truncated is True
    assert time.monotonic() - started < 2.5


def test_process_preserves_surrogateescape_input_and_explicit_environment(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UTILITY_INHERITED_SENTINEL", "must-not-inherit")
    result = run_process(
        _python(
            "import os, sys; "
            "sys.stdout.buffer.write((os.environ['UTILITY_EXPLICIT'] + '|' + "
            "os.environ.get('UTILITY_INHERITED_SENTINEL', 'missing') + '|').encode()); "
            "sys.stdout.buffer.write(sys.stdin.buffer.read())"
        ),
        cwd=tmp_path,
        input_text="café\n",
        env={"UTILITY_EXPLICIT": "set"},
        timeout_seconds=2,
    )

    assert result.stdout == "set|missing|café\n"
    assert result.stderr == ""

    invalid = run_process(
        _python("import sys; sys.stdout.buffer.write(b'good\\xff')"),
        cwd=tmp_path,
        timeout_seconds=2,
    )
    assert invalid.stdout == "good\udcff"


def test_process_returns_nonzero_and_classifies_signals_and_timeouts(
    tmp_path: pathlib.Path,
) -> None:
    result = run_process(
        _python("raise SystemExit(7)"),
        cwd=tmp_path,
        timeout_seconds=2,
    )
    assert result.returncode == 7

    with pytest.raises(RunnerSignalError) as signal_error:
        run_process(
            _python(
                "import os, signal, sys; "
                "print('signal-secret', file=sys.stderr, flush=True); "
                "os.kill(os.getpid(), signal.SIGTERM)"
            ),
            cwd=tmp_path,
            timeout_seconds=2,
        )
    assert signal_error.value._process_result.stderr == "signal-secret\n"
    assert "signal-secret" not in str(signal_error.value)

    with pytest.raises(RunnerTimeoutError) as timeout_error:
        run_process(
            _python(
                "import sys, time; "
                "print('timeout-secret', file=sys.stderr, flush=True); time.sleep(10)"
            ),
            cwd=tmp_path,
            timeout_seconds=2,
        )
    assert timeout_error.value._process_result.stderr == "timeout-secret\n"
    assert "timeout-secret" not in str(timeout_error.value)


@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup requires POSIX semantics")
def test_process_cleans_descendants_that_keep_pipes_open(tmp_path: pathlib.Path) -> None:
    pid_file = tmp_path / "descendant.pid"
    child_code = "import time; time.sleep(30)"
    parent_code = (
        "import subprocess, sys; "
        f"child = subprocess.Popen([{sys.executable!r}, '-c', {child_code!r}]); "
        f"open({str(pid_file)!r}, 'w', encoding='ascii').write(str(child.pid))"
    )

    started = time.monotonic()
    result = run_process(_python(parent_code), cwd=tmp_path, timeout_seconds=1)
    elapsed = time.monotonic() - started

    child_pid = int(pid_file.read_text(encoding="ascii"))
    assert result.returncode == 0
    assert elapsed < 2.5
    assert _wait_for_pid_exit(child_pid)
