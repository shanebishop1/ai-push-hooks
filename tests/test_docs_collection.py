from __future__ import annotations

import os
import pathlib

import pytest

import ai_push_hooks.modules.docs as docs_module
from ai_push_hooks.modules.docs import collect_docs_context

from .conftest import build_context, init_repo, make_config


def _collect(
    repo: pathlib.Path, *, changed_file: str, diff_text: str = ""
) -> dict[str, str]:
    config = make_config([])
    context = build_context(
        repo,
        config,
        changed_files=[changed_file],
        diff_text=diff_text,
    )
    return collect_docs_context(context, None).artifacts


def test_doc_inventory_rejects_external_and_dangling_symlinks(
    tmp_path: pathlib.Path,
) -> None:
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


def test_filename_derived_regex_metacharacters_are_literal(
    tmp_path: pathlib.Path,
) -> None:
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


def test_search_reads_each_document_once_and_preserves_query_ranking(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path)
    guide = repo / "docs" / "guide.md"
    guide.write_text(
        "first context\nfirst needle\nfirst trailing\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text(
        "second context\nsecond needle\nsecond trailing\n",
        encoding="utf-8",
    )
    doc_files = docs_module._expand_doc_files(repo)
    reads: list[pathlib.Path] = []
    original_read = docs_module._read_bounded_text

    def counted_read(path: pathlib.Path, max_bytes: int | None = None) -> str:
        reads.append(path)
        return original_read(path, max_bytes)

    monkeypatch.setattr(docs_module, "_read_bounded_text", counted_read)

    context = docs_module._search_docs_context(
        repo,
        doc_files,
        ["second needle", "first needle"],
    )

    assert reads == doc_files
    assert context.index("README.md:1: second context") < context.index(
        "docs/guide.md:1: first context"
    )
    assert "README.md:2: second needle" in context
    assert "docs/guide.md:2: first needle" in context


def test_no_match_preserves_rg_and_no_rg_semantics(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path)
    (repo / "README.md").write_text("fallback context\n", encoding="utf-8")
    doc_files = docs_module._expand_doc_files(repo)

    monkeypatch.setattr(docs_module.shutil, "which", lambda _name: "/usr/bin/rg")
    assert docs_module._search_docs_context(repo, doc_files, ["not-present"]) == ""

    monkeypatch.setattr(docs_module.shutil, "which", lambda _name: None)
    fallback = docs_module._search_docs_context(repo, doc_files, ["not-present"])
    assert "--- README.md ---\nfallback context" in fallback


def test_oversized_file_preserves_rg_skip_and_no_rg_bounded_prefix(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path)
    (repo / "README.md").write_text(
        "oversized-marker\n" + ("x" * 100), encoding="utf-8"
    )
    doc_files = docs_module._expand_doc_files(repo)
    monkeypatch.setattr(docs_module, "DOC_MAX_BYTES", 32)

    monkeypatch.setattr(docs_module.shutil, "which", lambda _name: "/usr/bin/rg")
    assert docs_module._search_docs_context(repo, doc_files, ["oversized-marker"]) == ""

    monkeypatch.setattr(docs_module.shutil, "which", lambda _name: None)
    bounded = docs_module._search_docs_context(repo, doc_files, ["oversized-marker"])
    assert "README.md:1: oversized-marker" in bounded


def test_heavy_matches_retain_bounded_metadata_and_read_each_file_once(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path)
    queries = [f"heavy-{index}" for index in range(20)]
    line = " ".join(queries)
    (repo / "README.md").write_text((line + "\n") * 10_000, encoding="utf-8")
    doc_files = [repo / "README.md"]
    reads: list[pathlib.Path] = []
    original_read = docs_module._read_bounded_text

    def counted_read(path: pathlib.Path, max_bytes: int | None = None) -> str:
        reads.append(path)
        return original_read(path, max_bytes)

    monkeypatch.setattr(docs_module, "_read_bounded_text", counted_read)
    buffers, _fallback_contents = docs_module._collect_query_matches(
        repo,
        doc_files,
        queries,
        rg_available=False,
    )

    assert reads == doc_files
    assert all(
        len(buffer.matches) <= docs_module.DOC_MAX_QUERY_MATCHES for buffer in buffers
    )
    assert sum(len(buffer.matches) for buffer in buffers) <= (
        len(queries) * docs_module.DOC_MAX_QUERY_MATCHES
    )
    assert all(
        buffer.characters
        <= docs_module.DOC_CONTEXT_BUDGET
        + max(
            (len(chunk) for _key, chunk in buffer.matches),
            default=0,
        )
        for buffer in buffers
    )


def test_long_matches_bound_characters_and_stop_after_first_file(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path)
    queries = [f"long-query-{index}" for index in range(20)]
    long_line = " ".join(queries) + ("x" * (docs_module.DOC_CONTEXT_BUDGET + 100))
    doc_files = [repo / "README.md"] + [
        repo / "docs" / f"long-{index:03d}.md" for index in range(25)
    ]
    for path in doc_files:
        path.write_text(long_line + "\n", encoding="utf-8")

    reads: list[pathlib.Path] = []
    original_read = docs_module._read_bounded_text

    def counted_read(path: pathlib.Path, max_bytes: int | None = None) -> str:
        reads.append(path)
        return original_read(path, max_bytes)

    monkeypatch.setattr(docs_module, "_read_bounded_text", counted_read)
    buffers, _fallback_contents = docs_module._collect_query_matches(
        repo,
        doc_files,
        queries,
        rg_available=False,
    )

    assert reads == [doc_files[0]]
    assert all(buffer.saturated for buffer in buffers)
    assert all(len(buffer.matches) == 1 for buffer in buffers)
    assert sum(buffer.characters for buffer in buffers) <= len(queries) * (
        docs_module.DOC_CONTEXT_BUDGET + len(long_line) + 64
    )
