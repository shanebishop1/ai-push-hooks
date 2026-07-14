from __future__ import annotations

import pytest

from ai_push_hooks import cli


@pytest.mark.parametrize("status", [0, 1, 17])
def test_hook_forwards_arguments_and_status(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
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
