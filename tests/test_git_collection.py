from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

from ai_push_hooks.executors import exec as exec_module
from ai_push_hooks.executors.exec import collect_changed_files, collect_diff
from ai_push_hooks.types import HookError

from .conftest import init_repo


def _git(repo: pathlib.Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _commit(repo: pathlib.Path, message: str) -> str:
    _git(repo, "add", "--", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def test_changed_files_preserve_unusual_names(tmp_path: pathlib.Path) -> None:
    repo = init_repo(tmp_path)
    raw_name = b" \tname with\nnewline-\xc3\xa9 \t"
    name = os.fsdecode(raw_name)
    path = repo / name
    path.write_bytes(b"content\n")
    commit = _commit(repo, "unusual filename")

    changed = collect_changed_files(repo, [f"HEAD~1..{commit}"])

    assert len(changed) == 1
    assert os.fsencode(changed[0]) == raw_name


def test_changed_files_preserve_surrogateescape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    escaped_name = "raw-\udcff-name"
    completed = subprocess.CompletedProcess(
        ["git", "diff"], 0, stdout=f"{escaped_name}\x00", stderr=""
    )
    monkeypatch.setattr(exec_module, "run_command", lambda *args, **kwargs: completed)

    assert collect_changed_files(pathlib.Path("."), ["range"]) == [escaped_name]


def test_collect_diff_enforces_encoded_byte_limit_for_multibyte_output(
    tmp_path: pathlib.Path,
) -> None:
    repo = init_repo(tmp_path)
    (repo / "multibyte.txt").write_text("🙂 café\n" * 5000, encoding="utf-8")
    commit = _commit(repo, "large multibyte file")
    maximum = 257

    diff = collect_diff(repo, [f"HEAD~1..{commit}"], maximum)

    encoded = diff.encode("utf-8", errors="surrogateescape")
    assert len(encoded) <= maximum
    assert "[diff truncated]" in diff


def test_collect_diff_bounds_large_output_and_preserves_range_headers(
    tmp_path: pathlib.Path,
) -> None:
    repo = init_repo(tmp_path)
    (repo / "first.txt").write_text("first\n" * 30000, encoding="utf-8")
    first = _commit(repo, "first large file")
    (repo / "second.txt").write_text("second\n" * 30000, encoding="utf-8")
    second = _commit(repo, "second large file")
    maximum = 512

    diff = collect_diff(repo, [f"HEAD~2..{first}", f"{first}..{second}"], maximum)

    assert len(diff.encode("utf-8", errors="surrogateescape")) <= maximum
    assert f"### RANGE HEAD~2..{first}" in diff
    assert "[diff truncated]" in diff


def test_collection_surfaces_git_errors(tmp_path: pathlib.Path) -> None:
    repo = init_repo(tmp_path)
    invalid_range = "not-a-revision..HEAD"

    with pytest.raises(HookError, match="Command failed"):
        collect_changed_files(repo, [invalid_range])
    with pytest.raises(HookError, match="Command failed"):
        collect_diff(repo, [invalid_range], 1024)


def test_collect_diff_keeps_multiple_range_sections(tmp_path: pathlib.Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "first.txt").write_text("first\n", encoding="utf-8")
    first = _commit(repo, "first")
    (repo / "second.txt").write_text("second\n", encoding="utf-8")
    second = _commit(repo, "second")
    ranges = [f"HEAD~2..{first}", f"{first}..{second}"]

    changed = collect_changed_files(repo, ranges)
    diff = collect_diff(repo, ranges, 10_000)

    assert changed == ["first.txt", "second.txt"]
    assert diff.count("### RANGE ") == 2
    assert f"### RANGE {ranges[0]}" in diff
    assert f"### RANGE {ranges[1]}" in diff
