from __future__ import annotations

import os
import pathlib
import re
import shutil
import stat
from pathlib import PurePosixPath
from typing import Any

from ..types import CollectorResult, RuntimeContext
from ..git_utils import collect_commit_messages_for_ranges, git, path_matches

DOC_INCLUDE_PATTERNS = ("README.md", "docs/**/*.md")
DOC_IGNORE_PATTERNS = ("docs/archive/**",)
DOC_CONTEXT_LINES = 2
DOC_MAX_BYTES = 64 * 1024
DOC_CONTEXT_BUDGET = 32000
DOC_FALLBACK_FILE_LIMIT = 8
# Matching metadata is deliberately bounded independently of the document
# inventory.  The normal context budget is reached much earlier, while this
# cap prevents a pathological repeated-match file from retaining every hit.
DOC_MAX_QUERY_MATCHES = 4096


def _path_matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(path_matches(path, pattern) for pattern in patterns)


def _is_safe_doc_file(repo_root: pathlib.Path, candidate: pathlib.Path) -> bool:
    """Return whether candidate is a contained, non-link regular file."""
    try:
        relative = candidate.relative_to(repo_root)
        current = repo_root
        for part in relative.parts:
            current /= part
            if stat.S_ISLNK(current.lstat().st_mode):
                return False
        candidate_stat = candidate.lstat()
        if not stat.S_ISREG(candidate_stat.st_mode):
            return False
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(repo_root)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _expand_doc_files(repo_root: pathlib.Path) -> list[pathlib.Path]:
    repo_root = repo_root.resolve(strict=True)
    files: list[pathlib.Path] = []
    for candidate in repo_root.rglob("*.md"):
        relative = candidate.relative_to(repo_root).as_posix()
        if not _path_matches(relative, DOC_INCLUDE_PATTERNS):
            continue
        if _path_matches(relative, DOC_IGNORE_PATTERNS):
            continue
        if not _is_safe_doc_file(repo_root, candidate):
            continue
        files.append(candidate)
    return sorted(files)


def _deterministic_seed_queries(diff_text: str, changed_files: list[str]) -> list[str]:
    stopwords = {
        "const",
        "return",
        "value",
        "false",
        "true",
        "string",
        "number",
        "object",
        "class",
        "function",
        "public",
        "private",
        "static",
        "async",
        "await",
        "import",
        "export",
        "from",
        "default",
        "docs",
        "readme",
    }
    seeds: list[str] = []
    for changed in changed_files:
        pure = PurePosixPath(changed)
        if len(pure.stem) >= 4:
            seeds.append(pure.stem)
        for segment in pure.parts:
            if len(segment) >= 4 and segment not in {"docs", "src", "tests"}:
                seeds.append(segment)
    seeds.extend(re.findall(r"\b[A-Za-z][A-Za-z0-9_.-]{3,}\b", diff_text))
    deduped: list[str] = []
    seen: set[str] = set()
    for seed in seeds:
        clean = seed.strip()
        if clean.lower() in stopwords or clean in seen:
            continue
        seen.add(clean)
        deduped.append(clean)
    return deduped[:20]


def _read_bounded_text(path: pathlib.Path, max_bytes: int | None = None) -> str:
    if max_bytes is None:
        max_bytes = DOC_MAX_BYTES
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return ""
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            return ""
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read(max_bytes).decode("utf-8", errors="replace")
    except (OSError, UnicodeError):
        return ""
    finally:
        if descriptor != -1:
            os.close(descriptor)


class _ContextAccumulator:
    """Collect bounded snippets without rescanning all prior snippets."""

    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.size = 0

    def append(self, chunk: str, budget: int) -> bool:
        remaining = budget - self.size
        separator_size = 1 if self.chunks else 0
        remaining -= separator_size
        if remaining <= 0:
            return False
        truncated = len(chunk) > remaining
        if truncated:
            if self.chunks:
                return False
            chunk = chunk[:remaining]
        self.chunks.append(chunk)
        self.size += separator_size + len(chunk)
        return not truncated

    def render(self) -> str:
        return "\n".join(self.chunks)


class _QueryMatchBuffer:
    """Bounded query-ranked snippet metadata for one document scan."""

    def __init__(self, max_matches: int = DOC_MAX_QUERY_MATCHES) -> None:
        self.max_matches = max_matches
        self.matches: list[tuple[tuple[int, int], str]] = []
        self._seen: set[tuple[int, int]] = set()
        self.characters = 0
        self.saturated = False

    def add(
        self,
        key: tuple[int, int],
        relative: str,
        line_number: int,
        line: str,
    ) -> None:
        if key in self._seen or self.saturated or len(self.matches) >= self.max_matches:
            return
        chunk = f"{relative}:{line_number}: {line}"
        self._seen.add(key)
        self.matches.append((key, chunk))
        self.characters += len(chunk)
        self.saturated = (
            len(self.matches) >= self.max_matches
            or self.characters >= DOC_CONTEXT_BUDGET
        )


def _fallback_docs_context(
    repo_root: pathlib.Path,
    doc_files: list[pathlib.Path],
    contents: dict[pathlib.Path, str] | None = None,
) -> str:
    snippets: list[str] = []
    for path in doc_files[:DOC_FALLBACK_FILE_LIMIT]:
        relative = path.relative_to(repo_root).as_posix()
        content = _read_bounded_text(path) if contents is None else contents[path]
        block = f"--- {relative} ---\n{content}"
        current_size = len("\n\n".join(snippets))
        remaining = DOC_CONTEXT_BUDGET - current_size
        if len(block) > remaining:
            if not snippets and remaining > 0:
                snippets.append(block[:remaining])
            break
        snippets.append(block)
    return "\n\n".join(snippets)


def _collect_query_matches(
    repo_root: pathlib.Path,
    doc_files: list[pathlib.Path],
    queries: list[str],
    *,
    rg_available: bool,
) -> tuple[list[_QueryMatchBuffer], dict[pathlib.Path, str]]:
    """Read each document once and retain only bounded ranked snippet metadata.

    The old rg path skipped files larger than ``DOC_MAX_BYTES`` while its
    no-rg fallback read a bounded prefix.  Keep that environment-dependent
    compatibility behavior explicit while avoiding N query subprocesses and
    without caching the whole documentation tree.
    """

    buffers = [_QueryMatchBuffer() for _query in queries]
    fallback_contents: dict[pathlib.Path, str] = {}
    for path_index, path in enumerate(doc_files):
        if rg_available:
            try:
                if path.stat().st_size > DOC_MAX_BYTES:
                    continue
            except OSError:
                continue
        content = _read_bounded_text(path)
        if not rg_available and path_index < DOC_FALLBACK_FILE_LIMIT:
            fallback_contents[path] = content
        lines = content.splitlines()
        relative = path.relative_to(repo_root).as_posix()
        for line_index, line in enumerate(lines):
            for query_index, query in enumerate(queries):
                if query not in line:
                    continue
                first = max(0, line_index - DOC_CONTEXT_LINES)
                last = min(len(lines), line_index + DOC_CONTEXT_LINES + 1)
                buffer = buffers[query_index]
                for index in range(first, last):
                    buffer.add(
                        (path_index, index),
                        relative,
                        index + 1,
                        lines[index],
                    )
                if all(buffer.saturated for buffer in buffers):
                    return buffers, fallback_contents
    return buffers, fallback_contents


def _search_docs_context(repo_root: pathlib.Path, doc_files: list[pathlib.Path], queries: list[str]) -> str:
    repo_root = repo_root.resolve(strict=True)
    if not doc_files:
        return ""
    if not queries:
        return _fallback_docs_context(repo_root, doc_files)

    rg_available = shutil.which("rg") is not None
    matches_by_query, fallback_contents = _collect_query_matches(
        repo_root,
        doc_files,
        queries,
        rg_available=rg_available,
    )
    accumulator = _ContextAccumulator()
    seen: set[tuple[int, int]] = set()
    for query_matches in matches_by_query:
        for key, chunk in query_matches.matches:
            if key in seen:
                continue
            seen.add(key)
            if not accumulator.append(chunk, DOC_CONTEXT_BUDGET):
                return accumulator.render()
    if accumulator.chunks:
        return accumulator.render()
    if rg_available:
        return ""
    return _fallback_docs_context(repo_root, doc_files, fallback_contents)


def collect_docs_context(context: RuntimeContext, _state: Any) -> CollectorResult:
    ranges = context.cache.get("ranges", [])
    changed_files = context.cache.get("changed_files", [])
    diff_text = context.cache.get("diff_text", "")
    doc_files = _expand_doc_files(context.repo_root)
    docs_context = _search_docs_context(
        context.repo_root,
        doc_files,
        _deterministic_seed_queries(diff_text, changed_files),
    )
    recent_commits = git(
        context.repo_root,
        ["log", "--oneline", "-n", "20", "--", "README.md", "docs"],
        check=False,
    )
    commits = collect_commit_messages_for_ranges(context.repo_root, ranges) if ranges else []
    commit_lines = []
    for commit in commits:
        commit_lines.append(f"--- {commit['hash']}")
        commit_lines.append(f"subject: {commit['subject']}")
        if commit["body"]:
            commit_lines.append("body:")
            commit_lines.append(commit["body"])
        commit_lines.append("")
    return CollectorResult(
        artifacts={
            "changed-files.txt": "\n".join(changed_files) + ("\n" if changed_files else ""),
            "push.diff": diff_text + ("\n" if diff_text and not diff_text.endswith("\n") else ""),
            "docs-inventory.txt": "\n".join(path.relative_to(context.repo_root).as_posix() for path in doc_files)
            + ("\n" if doc_files else ""),
            "docs-context.txt": docs_context + ("\n" if docs_context else ""),
            "recent-commits.txt": recent_commits + ("\n" if recent_commits and not recent_commits.endswith("\n") else ""),
            "commits.txt": "\n".join(commit_lines).strip() + ("\n" if commit_lines else ""),
        }
    )
