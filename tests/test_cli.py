from __future__ import annotations

import io
import json
import pathlib
import sys

import pytest

from ai_push_hooks import cli


@pytest.mark.parametrize("status", [0, 1, 17])
def test_hook_forwards_arguments_and_status(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    called_with: list[tuple[str, str]] = []

    def _run_hook(remote_name: str, remote_url: str) -> int:
        called_with.append((remote_name, remote_url))
        return status

    monkeypatch.setattr(cli, "run_hook", _run_hook)

    assert cli.main(["hook", "origin", "git@example.com:owner/repo.git"]) == status
    assert called_with == [("origin", "git@example.com:owner/repo.git")]


def test_hook_defaults_missing_git_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    called_with: list[tuple[str, str]] = []

    def _run_hook(remote_name: str, remote_url: str) -> int:
        called_with.append((remote_name, remote_url))
        return 0

    monkeypatch.setattr(cli, "run_hook", _run_hook)

    assert cli.main(["hook"]) == 0
    assert called_with == [("", "")]


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_error_class"),
    [
        ("nonzero", "status=7", "StepCommandAssertionError"),
        ("timeout", "status=timeout", "RunnerTimeoutError"),
    ],
)
def test_real_hook_cli_keeps_failed_command_streams_private(
    repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
    expected_status: str,
    expected_error_class: str,
) -> None:
    marker = "synthetic-private-command-marker"
    if failure == "nonzero":
        script = (
            "import sys; sys.stdout.write('"
            + marker
            + "-stdout'); sys.stderr.write('"
            + marker
            + "-stderr'); raise SystemExit(7)"
        )
        timeout_setting = ""
    else:
        script = (
            "import sys, time; sys.stdout.write('"
            + marker
            + "-stdout'); sys.stdout.flush(); sys.stderr.write('"
            + marker
            + "-stderr'); sys.stderr.flush(); time.sleep(10)"
        )
        timeout_setting = "timeout_seconds = 1\n"
    command = json.dumps([sys.executable, "-c", script])
    (repo / "ai-push-hooks.toml").write_text(
        "[general]\n"
        "skip_on_sync_branch = false\n"
        "\n"
        "[logging]\n"
        "jsonl = false\n"
        "\n"
        "[workflow]\n"
        'modules = ["gate"]\n'
        "\n"
        "[modules.gate]\n"
        "enabled = true\n"
        "\n"
        "[[modules.gate.steps]]\n"
        f'id = "{failure}-check"\n'
        'type = "assert"\n'
        f"{timeout_setting}"
        f"command = {command}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    assert cli.main(["hook"]) == 1

    captured = capsys.readouterr()
    assert marker not in captured.out
    assert marker not in captured.err
    assert "Traceback" not in captured.err
    assert f"gate.{failure}-check" in captured.err
    assert expected_status in captured.err
    assert f"error_class={expected_error_class}" in captured.err
    assert "artifact_location=" in captured.err
    assert str(repo) not in captured.err
    assert sys.executable not in captured.err

    runs = sorted((repo / ".git" / "ai-push-hooks" / "runs").iterdir())
    assert len(runs) == 1
    step_dir = runs[0] / "gate" / f"00-{failure}-check"
    assert f"artifact_location={runs[0].name}/gate/00-{failure}-check" in captured.err
    assert (step_dir / "stdout.txt").read_bytes() == f"{marker}-stdout".encode()
    assert (step_dir / "stderr.txt").read_bytes() == f"{marker}-stderr".encode()
    report = json.loads((step_dir / "result.json").read_text(encoding="utf-8"))
    assert marker not in json.dumps(report)
