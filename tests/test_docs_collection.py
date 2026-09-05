from __future__ import annotations

import os
import pathlib

import pytest

import ai_push_hooks.modules.docs as docs_module
from ai_push_hooks.modules.docs import collect_docs_context

from .conftest import build_context, init_repo, make_config


def _collect(repo: pathlib.Path, *, changed_file: str, diff_text: str = "") -> dict[str, str]:
    config = make_config([])
    context = build_context(
        repo,
        config,
        changed_files=[changed_file],
        diff_text=diff_text,
    )
    return collect_docs_context(context, None).artifacts


def test_doc_inventory_rejects_external_and_dangling_symlinks(tmp_path: pathlib.Path) -> None:
    repo = init_repo(tmp_path)
    external = tmp_path / "external.md"
    external.write_text("external secret marker\n", encoding="utf-8")
    (repo / "docs" / "external.md").symlink_to(external)
    (repo / "docs" / "dangling.md").symlink_to(tmp_path / "missing.md")

    artifacts = _collect(repo, changed_file="src/external.py")

    assert "docs/external.md" not in artifacts["docs-inventory.txt"]
    assert "docs/dangling.md" not in artifacts["docs-inventory.txt"]
    assert "external secret marker" not in artifacts["docs-context.txt"]


def test_doc_inventory_rejects_directories_and_fifos(tmp_path: pathlib.Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "docs" / "directory.md").mkdir()
    fifo = repo / "docs" / "pipe.md"
    os.mkfifo(fifo)

    artifacts = _collect(repo, changed_file="src/pipe.py")

    assert "docs/directory.md" not in artifacts["docs-inventory.txt"]
    assert "docs/pipe.md" not in artifacts["docs-inventory.txt"]


def test_readme_only_search_includes_filename(tmp_path: pathlib.Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "README.md").write_text("README-only marker\n", encoding="utf-8")

    artifacts = _collect(repo, changed_file="src/README-only.py")

    assert "README.md:1: README-only marker" in artifacts["docs-context.txt"]


def test_search_retains_surrounding_context(tmp_path: pathlib.Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "README.md").write_text(
        "before context-target\ncontext-target\nafter context-target\n",
        encoding="utf-8",
    )

    artifacts = _collect(repo, changed_file="src/context-target.py")
    context = artifacts["docs-context.txt"]

    assert "README.md:1: before context-target" in context
    assert "README.md:2: context-target" in context
    assert "README.md:3: after context-target" in context


def test_filename_derived_regex_metacharacters_are_literal(tmp_path: pathlib.Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "README.md").write_text("literal [needle marker\n", encoding="utf-8")

    artifacts = _collect(repo, changed_file="src/[needle.py")

    assert "README.md:1: literal [needle marker" in artifacts["docs-context.txt"]


def test_fallback_reads_only_the_bounded_prefix(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path)
    (repo / "README.md").write_text("0123456789ABCDEFGHIJ", encoding="utf-8")
    monkeypatch.setattr(docs_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(docs_module, "DOC_MAX_BYTES", 10)

    artifacts = _collect(repo, changed_file="src/prefix.py")

    assert "--- README.md ---\n0123456789" in artifacts["docs-context.txt"]
    assert "ABCDEFGHIJ" not in artifacts["docs-context.txt"]
