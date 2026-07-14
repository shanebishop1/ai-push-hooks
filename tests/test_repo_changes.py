from __future__ import annotations

import pathlib

from ai_push_hooks.executors.exec import list_repo_changes


def test_list_repo_changes_preserves_first_path_character(repo: pathlib.Path) -> None:
    docs_index = repo / "docs" / "INDEX.md"
    docs_index.write_text("# Updated Docs Index\n", encoding="utf-8")

    assert list_repo_changes(repo) == {"docs/INDEX.md"}
