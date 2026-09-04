from __future__ import annotations

import pathlib
import subprocess

from ai_push_hooks.executors import exec as exec_module
from ai_push_hooks.executors.exec import list_repo_changes


def test_list_repo_changes_preserves_first_path_character(repo: pathlib.Path) -> None:
    docs_index = repo / "docs" / "INDEX.md"
    docs_index.write_text("# Updated Docs Index\n", encoding="utf-8")

    assert list_repo_changes(repo) == {"docs/INDEX.md"}


def test_list_repo_changes_returns_both_paths_for_rename(repo: pathlib.Path) -> None:
    subprocess.run(
        ["git", "mv", "README.md", "renamed.md"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )

    assert list_repo_changes(repo) == {"README.md", "renamed.md"}


def test_list_repo_changes_decodes_non_utf8_names_with_surrogateescape(
    repo: pathlib.Path,
    monkeypatch,
) -> None:
    raw_name = b"invalid-\xff.txt"
    decoded_name = raw_name.decode("utf-8", errors="surrogateescape")
    monkeypatch.setattr(
        exec_module,
        "run_command",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=f"?? {decoded_name}\x00", stderr=""
        ),
    )

    changes = list_repo_changes(repo)

    assert len(changes) == 1
    assert next(iter(changes)).encode("utf-8", errors="surrogateescape") == raw_name
