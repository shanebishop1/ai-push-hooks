from __future__ import annotations

import pathlib
import subprocess

import pytest

from ai_push_hooks import hook as hook_module
from ai_push_hooks.executors.exec import (
    collect_changed_files,
    collect_commit_messages_for_ranges,
    collect_ranges_from_stdin,
    parse_push_updates,
    path_matches,
)
from ai_push_hooks.modules.beads import collect_beads_status_context
from ai_push_hooks.types import HookError, WorkflowRunResult

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


def _commit_file(repo: pathlib.Path, relative_path: str, content: str, message: str) -> str:
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", relative_path)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _capture_hook_context(
    repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    stdin_lines: list[str],
    remote: str = "https://example.com/org/repo.git",
):
    captured = {}

    class CapturingEngine:
        def __init__(self, context, artifacts):
            captured["context"] = context

        def run(self):
            context = captured["context"]
            return WorkflowRunResult(run_dir=context.run_dir, modules={})

    monkeypatch.setattr(hook_module, "WorkflowEngine", CapturingEngine)

    assert hook_module.run_hook(remote, remote, stdin_lines=stdin_lines, cwd=repo) == 0
    return captured["context"]


def test_hook_uses_single_non_head_pushed_branch_and_preserves_update(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-b", "feature/source")
    _commit_file(repo, "src/first.py", "first = True\n", "first feature commit")
    tip = _commit_file(repo, "src/second.py", "second = True\n", "second feature commit")
    _git(repo, "checkout", "main")
    zero = "0" * len(tip)

    context = _capture_hook_context(
        repo,
        monkeypatch,
        [f"refs/heads/feature/source {tip} refs/heads/feature/pushed {zero}"],
    )

    update = context.cache["push_updates"][0]
    assert (
        update.local_ref,
        update.local_sha,
        update.remote_ref,
        update.remote_sha,
    ) == (
        "refs/heads/feature/source",
        tip,
        "refs/heads/feature/pushed",
        zero,
    )
    assert update.ref_kind == "branch"
    assert update.operation == "create"
    assert context.cache["branch_name"] == "feature/pushed"
    assert context.cache["checked_out_branch"] == "main"
    assert context.cache["branch_ranges"] == [f"{base}..{tip}"]
    assert context.cache["branch_changed_files"] == ["src/first.py", "src/second.py"]
    commits = collect_commit_messages_for_ranges(repo, context.cache["branch_ranges"])
    assert {commit["subject"] for commit in commits} == {
        "first feature commit",
        "second feature commit",
    }

    beads_result = collect_beads_status_context(context, object())
    assert beads_result.skip_module is False
    assert "branch=feature/pushed\n" in str(beads_result.artifacts["branch-context.txt"])


def test_multiple_pushed_branches_fail_closed_before_workflow(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path)
    zero = "0" * 40
    branch_tips = {}
    for branch in ("feature/one", "feature/two"):
        _git(repo, "checkout", "main")
        _git(repo, "checkout", "-b", branch)
        branch_tips[branch] = _commit_file(
            repo,
            f"src/{branch.rsplit('/', 1)[-1]}.py",
            f"branch = {branch!r}\n",
            f"add {branch}",
        )
    _git(repo, "checkout", "main")
    lines = [
        f"refs/heads/{branch} {tip} refs/heads/{branch} {zero}"
        for branch, tip in branch_tips.items()
    ]

    with pytest.raises(HookError, match="multiple branch updates"):
        _capture_hook_context(repo, monkeypatch, lines)


def test_setup_failure_honors_environment_fail_open(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path)
    monkeypatch.setenv("AI_PUSH_HOOKS_ALLOW_PUSH_ON_ERROR", "1")

    assert hook_module.run_hook(stdin_lines=["malformed"], cwd=repo) == 0


def test_setup_failure_honors_configured_fail_open(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path)
    config_path = repo / "ai-push-hooks.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "allow_push_on_error = false", "allow_push_on_error = true"
        ),
        encoding="utf-8",
    )

    assert hook_module.run_hook(stdin_lines=["malformed"], cwd=repo) == 0


def test_skip_override_bypasses_repository_and_config_setup(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI_PUSH_HOOKS_SKIP", "1")

    assert hook_module.run_hook(cwd=tmp_path) == 0


def test_tag_and_deletion_updates_are_preserved_without_selecting_a_branch(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path)
    _git(repo, "tag", "v1")
    tag_oid = _git(repo, "rev-parse", "refs/tags/v1")
    existing_oid = _git(repo, "rev-parse", "HEAD")
    zero = "0" * len(tag_oid)

    context = _capture_hook_context(
        repo,
        monkeypatch,
        [
            f"refs/tags/v1 {tag_oid} refs/tags/v1 {zero}",
            f"(delete) {zero} refs/heads/obsolete {existing_oid}",
        ],
    )

    tag_update, deletion = context.cache["push_updates"]
    assert (tag_update.ref_kind, tag_update.operation) == ("tag", "create")
    assert (deletion.ref_kind, deletion.operation) == ("branch", "delete")
    assert context.cache["pushed_branches"] == []
    assert context.cache["branch_name"] == ""
    assert context.cache["branch_selection_reason"] == "no pushed branch updates"


def test_deletion_only_push_never_falls_back_to_head(tmp_path: pathlib.Path) -> None:
    repo = init_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    zero = "0" * len(head)

    ranges = collect_ranges_from_stdin(
        repo,
        "origin",
        [f"(delete) {zero} refs/heads/obsolete {head}"],
    )

    assert ranges == []


def test_root_new_branch_without_base_uses_hash_format_empty_tree(
    tmp_path: pathlib.Path,
) -> None:
    repo = init_repo(tmp_path)
    _git(repo, "branch", "-m", "feature/root")
    root = _git(repo, "rev-parse", "HEAD")
    zero = "0" * len(root)
    empty_tree = _git(repo, "hash-object", "-t", "tree", "/dev/null")

    ranges = collect_ranges_from_stdin(
        repo,
        "https://example.com/org/repo.git",
        [f"refs/heads/feature/root {root} refs/heads/feature/root {zero}"],
        base_branch="missing-base",
    )

    assert ranges == [f"{empty_tree}..{root}"]
    assert "README.md" in collect_changed_files(repo, ranges)
    commits = collect_commit_messages_for_ranges(repo, ranges)
    assert [commit["hash"] for commit in commits] == [root]


def test_existing_remote_object_builds_exact_range_and_missing_object_fails_closed(
    tmp_path: pathlib.Path,
) -> None:
    repo = init_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature/ranges")
    first = _commit_file(repo, "src/first.py", "first = 1\n", "first")
    tip = _commit_file(repo, "src/second.py", "second = 2\n", "second")

    existing_ranges = collect_ranges_from_stdin(
        repo,
        "origin",
        [f"refs/heads/feature/ranges {tip} refs/heads/feature/ranges {first}"],
    )
    assert existing_ranges == [f"{first}..{tip}"]
    assert collect_changed_files(repo, existing_ranges) == ["src/second.py"]
    with pytest.raises(HookError, match="Advertised remote commit is unavailable"):
        collect_ranges_from_stdin(
            repo,
            "https://example.com/org/repo.git",
            [f"refs/heads/feature/ranges {tip} refs/heads/feature/ranges {'f' * 40}"],
        )


def test_parser_accepts_sha256_zero_oid_and_preserves_source_expression() -> None:
    local_oid = "a" * 64
    zero = "0" * 64

    updates = parse_push_updates(
        [f"HEAD~ {local_oid} refs/heads/feature/sha256 {zero}"]
    )

    assert updates[0].local_ref == "HEAD~"
    assert updates[0].local_sha == local_oid
    assert updates[0].remote_sha == zero
    assert updates[0].operation == "create"
    assert updates[0].branch_name == "feature/sha256"


def test_sha256_root_range_uses_sha256_empty_tree(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "sha256-repo"
    repo.mkdir()
    initialized = subprocess.run(
        ["git", "init", "--object-format=sha256", "-b", "feature/root"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if initialized.returncode != 0:
        pytest.skip("installed Git does not support SHA-256 repositories")
    _git(repo, "config", "user.email", "codex@example.com")
    _git(repo, "config", "user.name", "Codex")
    (repo / "root.txt").write_text("root\n", encoding="utf-8")
    _git(repo, "add", "root.txt")
    _git(repo, "commit", "-m", "root")
    root = _git(repo, "rev-parse", "HEAD")
    empty_tree = _git(repo, "hash-object", "-t", "tree", "/dev/null")

    ranges = collect_ranges_from_stdin(
        repo,
        "https://example.com/org/repo.git",
        [
            f"refs/heads/feature/root {root} refs/heads/feature/root "
            f"{'0' * len(root)}"
        ],
        base_branch="missing-base",
    )

    assert len(root) == 64
    assert len(empty_tree) == 64
    assert ranges == [f"{empty_tree}..{root}"]
    assert collect_changed_files(repo, ranges) == ["root.txt"]


@pytest.mark.parametrize(
    "line",
    [
        "refs/heads/main only-three fields",
        f"refs/heads/main {'a' * 39} refs/heads/main {'0' * 39}",
        f"refs/heads/main {'g' * 40} refs/heads/main {'0' * 40}",
    ],
)
def test_parser_rejects_malformed_hook_input(line: str) -> None:
    with pytest.raises(HookError, match="Malformed pre-push input"):
        parse_push_updates([line])


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("README.md", "README.md", True),
        ("nested/README.md", "README.md", False),
        ("root.md", "*.md", True),
        ("nested/root.md", "*.md", False),
        ("root.md", "**/*.md", True),
        ("docs/INDEX.md", "docs/**/*.md", True),
        ("docs/guides/setup.md", "docs/**/*.md", True),
        ("other/docs/setup.md", "docs/**/*.md", False),
        ("docs/guides/setup.txt", "docs/**/*.md", False),
    ],
)
def test_path_globs_are_root_anchored_and_segment_aware(
    path: str, pattern: str, expected: bool
) -> None:
    assert path_matches(path, pattern) is expected


@pytest.mark.parametrize("other_ref", ["refs/tags/v1", "refs/notes/review"])
def test_sync_branch_mixed_with_non_branch_ref_does_not_skip(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    other_ref: str,
) -> None:
    repo = init_repo(tmp_path)
    _git(repo, "checkout", "-b", "beads-sync")
    tip = _commit_file(repo, "sync.txt", "sync\n", "sync")
    zero = "0" * len(tip)

    context = _capture_hook_context(
        repo,
        monkeypatch,
        [
            f"refs/heads/beads-sync {tip} refs/heads/beads-sync {zero}",
            f"{other_ref} {tip} {other_ref} {zero}",
        ],
    )

    assert context.cache["branch_name"] == "beads-sync"
    assert len(context.cache["push_updates"]) == 2
