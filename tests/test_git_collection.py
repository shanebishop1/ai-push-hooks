from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

from ai_push_hooks import git_utils
from ai_push_hooks.artifacts import ArtifactStore
from ai_push_hooks.engine import WorkflowEngine
from ai_push_hooks.git_utils import collect_changed_files, collect_diff
from ai_push_hooks.types import HookError, ModuleConfig, StepConfig

from .conftest import build_context, init_repo, make_config


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
    monkeypatch.setattr(git_utils, "run_command", lambda *args, **kwargs: completed)

    assert collect_changed_files(pathlib.Path("."), ["range"]) == [escaped_name]


def test_collect_diff_enforces_encoded_byte_limit_for_multibyte_output(
    tmp_path: pathlib.Path,
) -> None:
    repo = init_repo(tmp_path)
    (repo / "multibyte.txt").write_text("🙂 café\n" * 5000, encoding="utf-8")
    commit = _commit(repo, "large multibyte file")
    maximum = 257

    diff = collect_diff(repo, [f"HEAD~1..{commit}"], maximum)

    encoded = diff.encode("utf-8")
    assert len(encoded) <= maximum
    assert "[diff truncated]" in diff

    artifact = tmp_path / "push.diff"
    assert git_utils.write_text_file(artifact, diff)
    assert artifact.read_text(encoding="utf-8") == diff


@pytest.mark.parametrize(
    ("character", "cut_bytes"),
    [
        ("é", 1),
        ("€", 1),
        ("€", 2),
        ("🙂", 1),
        ("🙂", 2),
        ("🙂", 3),
    ],
)
def test_collect_diff_drops_cut_utf8_character_before_truncation_marker(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    character: str,
    cut_bytes: int,
) -> None:
    prefix = "### RANGE range\n"
    marker = "\n[diff truncated]\n"
    payload = b"prefix "
    character_bytes = character.encode()
    assert 0 < cut_bytes < len(character_bytes)
    maximum = (
        len(prefix.encode("utf-8"))
        + len(payload)
        + cut_bytes
        + len(marker.encode("utf-8"))
    )

    monkeypatch.setattr(
        git_utils,
        "_collect_bounded_git_diff",
        lambda *_args: (f"prefix {character} suffix".encode(), True),
    )

    diff = collect_diff(tmp_path, ["range"], maximum)

    assert diff == prefix + "prefix " + marker
    assert len(diff.encode("utf-8")) <= maximum
    artifact = tmp_path / "push.diff"
    assert git_utils.write_text_file(artifact, diff)
    assert artifact.read_text(encoding="utf-8") == diff


def test_collect_diff_marks_malformed_replacement_overflow_as_truncated(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = "### RANGE range\n"
    marker = "\n[diff truncated]\n"
    body = b"ok" + b"\xff" * 6
    maximum = len(prefix.encode()) + len(b"ok") + len(marker.encode())
    raw_output = prefix.encode() + body + b"\n"
    normalized_output = prefix + body.decode("utf-8", errors="replace") + "\n"
    assert len(raw_output) <= maximum
    assert len(normalized_output.encode("utf-8")) > maximum
    monkeypatch.setattr(
        git_utils,
        "_collect_bounded_git_diff",
        lambda *_args: (body, False),
    )

    diff = collect_diff(tmp_path, ["range"], maximum)

    assert diff == prefix + "ok" + marker
    assert len(diff.encode("utf-8")) <= maximum


@pytest.mark.parametrize(
    ("source_truncated", "expected"),
    [(True, "prefix "), (False, "prefix �")],
)
def test_decode_diff_output_distinguishes_cut_suffix_from_malformed_eof(
    source_truncated: bool, expected: str
) -> None:
    diff = git_utils._decode_diff_output(b"prefix \xf0\x9f", 100, source_truncated)

    assert diff.startswith(expected)
    assert len(diff.encode("utf-8")) <= 100
    if source_truncated:
        assert diff.endswith(git_utils.DIFF_TRUNCATION_MARKER)
    else:
        assert "[diff truncated]" not in diff


@pytest.mark.parametrize(
    ("body", "source_truncated", "expected_body"),
    [
        (b"malformed \xff diff", False, "malformed � diff"),
        (b"prefix \xf0\x9f", True, "prefix "),
    ],
)
def test_builtin_collector_persists_diff_through_artifact_store(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    source_truncated: bool,
    expected_body: str,
) -> None:
    repo = init_repo(tmp_path)
    prefix = "### RANGE range\n"
    marker = "\n[diff truncated]\n"
    maximum = (
        100
        if not source_truncated
        else len(prefix.encode()) + len(body) + len(marker.encode())
    )
    monkeypatch.setattr(
        git_utils,
        "_collect_bounded_git_diff",
        lambda *_args: (body, source_truncated),
    )
    diff = collect_diff(repo, ["range"], maximum)
    module = ModuleConfig(
        id="docs",
        enabled=True,
        steps=(StepConfig(id="collect", type="collect", collector="docs_context"),),
    )
    context = build_context(repo, make_config([module]), diff_text=diff)

    result = WorkflowEngine(context, ArtifactStore(context.run_dir)).run()

    assert result.modules == {"docs": "completed"}
    expected = prefix + expected_body + (marker if source_truncated else "\n")
    assert diff == expected
    assert len(diff.encode("utf-8")) <= maximum
    artifact = context.run_dir / "docs" / "00-collect" / "push.diff"
    assert artifact.read_text(encoding="utf-8") == diff


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

    assert len(diff.encode("utf-8")) <= maximum
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
